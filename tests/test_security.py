"""Service-to-service authentication.

The OIDC path is exercised with ``_verify_oidc_token`` patched out: the thing
worth testing here is our own policy (is an audience configured, is the caller
on the allowlist, does a bad token become a 401), not Google's signature
verification, which would need network and real keys to reach.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.core.roles import Role
from app.db.firestore import FirestoreDB
from app.main import build_services, create_app
from app.services.publisher import PublisherService
from tests.conftest import FakePublisherClient
from tests.fake_firestore import FakeFirestoreClient


def _client_with(settings: Settings) -> Iterator[TestClient]:
    database = FirestoreDB(settings, client=FakeFirestoreClient())  # type: ignore[arg-type]
    publisher = PublisherService(settings, client=FakePublisherClient())  # type: ignore[arg-type]

    app = create_app()

    @asynccontextmanager
    async def lifespan(inner: FastAPI):  # type: ignore[no-untyped-def]
        build_services(inner, settings, database, publisher=publisher)
        yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as test_client:
        yield test_client


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "gcp_project_id": "test-project",
        "pubsub_job_topic_id": "test-jobs-topic",
        "auth_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


# --- Health stays open ------------------------------------------------------


def test_health_is_reachable_without_a_token() -> None:
    """Cloud Run's probes carry no identity token; gating health breaks deploys."""

    settings = _settings(auth_audience="https://mw.example.run.app")
    for client in _client_with(settings):
        assert client.get("/health/live").status_code == 200


# --- Missing / malformed credentials ---------------------------------------


def test_protected_route_rejects_a_missing_token() -> None:
    settings = _settings(auth_audience="https://mw.example.run.app")
    for client in _client_with(settings):
        response = client.get("/agents")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_protected_route_rejects_an_invalid_oidc_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(token: str, audience: str) -> dict[str, Any]:
        raise ValueError("Token expired")

    monkeypatch.setattr(security, "_verify_oidc_token", boom)

    settings = _settings(auth_audience="https://mw.example.run.app")
    for client in _client_with(settings):
        response = client.get("/agents", headers={"Authorization": "Bearer not-a-real-token"})
        assert response.status_code == 401


# --- The audience is mandatory ---------------------------------------------


def test_missing_audience_is_a_500_not_an_open_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an audience a token cannot be bound to this service.

    Google issues valid tokens to every account on every project, so verifying
    signature alone would accept essentially anything. Refusing loudly is the
    only safe direction; the regression this guards against is somebody
    "fixing" the misconfiguration by passing audience=None.
    """

    def should_not_run(token: str, audience: str) -> dict[str, Any]:
        raise AssertionError("verification must not be attempted without an audience")

    monkeypatch.setattr(security, "_verify_oidc_token", should_not_run)

    settings = _settings(auth_audience=None)
    for client in _client_with(settings):
        response = client.get("/agents", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 500


# --- A verified caller -------------------------------------------------------


def test_a_verified_token_is_allowed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def verify(token: str, audience: str) -> dict[str, Any]:
        seen["token"] = token
        seen["audience"] = audience
        return {"sub": "1234", "email": "portal@karoscmo.iam.gserviceaccount.com"}

    monkeypatch.setattr(security, "_verify_oidc_token", verify)

    settings = _settings(auth_audience="https://mw.example.run.app")
    for client in _client_with(settings):
        response = client.get("/agents", headers={"Authorization": "Bearer good-token"})
        assert response.status_code == 200, response.text

    assert seen["token"] == "good-token"
    # The configured audience must actually reach the verifier.
    assert seen["audience"] == "https://mw.example.run.app"


def test_allowlist_rejects_an_unlisted_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        security,
        "_verify_oidc_token",
        lambda token, audience: {"sub": "1", "email": "stranger@evil.iam.gserviceaccount.com"},
    )

    settings = _settings(
        auth_audience="https://mw.example.run.app",
        auth_allowed_service_accounts=["portal@karoscmo.iam.gserviceaccount.com"],
    )
    for client in _client_with(settings):
        response = client.get("/agents", headers={"Authorization": "Bearer good-signature"})
        # 403, not 401: the token is genuine, the caller just isn't permitted.
        assert response.status_code == 403


def test_allowlist_admits_a_listed_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        security,
        "_verify_oidc_token",
        lambda token, audience: {"sub": "1", "email": "portal@karoscmo.iam.gserviceaccount.com"},
    )

    settings = _settings(
        auth_audience="https://mw.example.run.app",
        auth_allowed_service_accounts=["portal@karoscmo.iam.gserviceaccount.com"],
    )
    for client in _client_with(settings):
        assert client.get("/agents", headers={"Authorization": "Bearer ok"}).status_code == 200


# --- The development bearer token -------------------------------------------


def test_dev_token_is_accepted_outside_production() -> None:
    settings = _settings(environment="local", auth_dev_token="s3cret", auth_audience=None)
    for client in _client_with(settings):
        assert client.get("/agents", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_dev_token_is_ignored_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray AUTH_DEV_TOKEN on a production deploy must not be a bypass."""

    def reject(token: str, audience: str) -> dict[str, Any]:
        raise ValueError("not a real OIDC token")

    monkeypatch.setattr(security, "_verify_oidc_token", reject)

    settings = _settings(
        environment="production",
        auth_dev_token="s3cret",
        auth_audience="https://mw.example.run.app",
    )
    for client in _client_with(settings):
        # Presented as a bearer token it is no longer special: it falls through
        # to OIDC verification, which rejects it.
        response = client.get("/agents", headers={"Authorization": "Bearer s3cret"})
        assert response.status_code == 401


def test_a_wrong_dev_token_still_falls_through_to_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev token being configured must not shadow real OIDC callers."""

    monkeypatch.setattr(
        security,
        "_verify_oidc_token",
        lambda token, audience: {"sub": "9", "email": "real@karoscmo.iam.gserviceaccount.com"},
    )

    settings = _settings(
        environment="local",
        auth_dev_token="s3cret",
        auth_audience="https://mw.example.run.app",
    )
    for client in _client_with(settings):
        response = client.get("/agents", headers={"Authorization": "Bearer a-real-oidc-token"})
        assert response.status_code == 200


# --- The escape hatch --------------------------------------------------------


def test_auth_can_be_disabled_entirely_for_local_development() -> None:
    settings = _settings(auth_enabled=False)
    for client in _client_with(settings):
        assert client.get("/agents").status_code == 200


# --- Roles -------------------------------------------------------------------
#
# Authentication answers "who is this". These answer "and what may they do",
# which the service had no concept of: CallerIdentity was written to
# request.state.caller and nothing ever read it, so every verified caller could
# do everything, and `created_by` / `?actor=` were free text the caller chose
# for itself.


PORTAL = "portal@karoscmo.iam.gserviceaccount.com"


def _as(monkeypatch: pytest.MonkeyPatch, email: str) -> None:
    """Make every verified token resolve to ``email``."""

    monkeypatch.setattr(
        security,
        "_verify_oidc_token",
        lambda token, audience: {"sub": "sub-" + email, "email": email},
    )


def _bound(role: str, **overrides: Any) -> Settings:
    return _settings(
        auth_audience="https://mw.example.run.app",
        auth_role_bindings={PORTAL: role},
        **overrides,
    )


HEADERS = {"Authorization": "Bearer good-token"}


def test_role_ordering_is_a_floor_not_an_enumeration() -> None:
    # A route names the MINIMUM it needs. Enumerating accepted roles instead is
    # how a new role gets forgotten at one of fifty-one call sites.
    assert Role.ADMIN.satisfies(Role.VIEWER)
    assert Role.ADMIN.satisfies(Role.EDITOR)
    assert Role.EDITOR.satisfies(Role.VIEWER)
    assert not Role.EDITOR.satisfies(Role.ADMIN)
    assert not Role.VIEWER.satisfies(Role.EDITOR)


def test_a_viewer_can_read_and_cannot_write(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, PORTAL)
    for client in _client_with(_bound("viewer")):
        assert client.get("/agents", headers=HEADERS).status_code == 200

        refused = client.post(
            "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
        )
        assert refused.status_code == 403, refused.text
        # The principal and both roles are named: a missing binding is the most
        # likely cause of a refusal here and it is invisible from the client
        # side, so a bare "forbidden" sends someone to the wrong config file.
        detail = refused.json()["detail"]
        assert PORTAL in detail
        assert "viewer" in detail and "editor" in detail


def test_an_editor_can_write_and_cannot_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, PORTAL)
    for client in _client_with(_bound("editor")):
        created = client.post(
            "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
        )
        assert created.status_code == 201, created.text

        # An ordinary edit is an editor's job.
        assert (
            client.patch("/agents/writer", json={"name": "Renamed"}, headers=HEADERS).status_code
            == 200
        )

        # Deleting an agent, and disabling one, are admin: both silently stop
        # every client that depends on it and nothing in the run path says why.
        assert client.delete("/agents/writer", headers=HEADERS).status_code == 403
        assert (
            client.patch(
                "/agents/writer/status", json={"status": "disabled"}, headers=HEADERS
            ).status_code
            == 403
        )

        # ...and the same through the general edit route, which also accepts
        # `status`. Gating only the dedicated route would put the check on the
        # tidier of two doors into the same room.
        refused = client.patch(
            "/agents/writer", json={"status": "disabled"}, headers=HEADERS
        )
        assert refused.status_code == 403, refused.text
        assert "admin" in refused.json()["detail"]


def test_an_admin_can_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, PORTAL)
    for client in _client_with(_bound("admin")):
        assert (
            client.post(
                "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
            ).status_code
            == 201
        )
        assert client.delete("/agents/writer", headers=HEADERS).status_code == 200
        assert client.post("/agents/writer/restore", headers=HEADERS).status_code == 200


def test_an_unbound_caller_falls_back_to_the_default_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Once ANY binding exists, a caller nobody bound fails toward less
    # authority. That is what makes a forgotten binding a refused write with
    # the principal named, rather than a silent grant.
    _as(monkeypatch, "someone-else@karoscmo.iam.gserviceaccount.com")
    for client in _client_with(_bound("admin")):
        assert client.get("/agents", headers=HEADERS).status_code == 200
        assert (
            client.post(
                "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
            ).status_code
            == 403
        )


def test_no_bindings_at_all_means_authorization_is_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty binding map must not change behaviour. This is the one that
    stops this feature taking production down.

    AUTH_ENABLED is hardcoded true in cloudbuild.yaml for BOTH environments, so
    every caller here is already a verified principal -- unlike agent-engine,
    where auth is still off. If an unbound caller fell to `viewer` while the map
    was empty, the first deploy carrying this code would 403 every write the
    portal makes: create, update, dispatch, run callbacks.

    So an empty map means authorization is not configured, and every verified
    caller holds admin exactly as it did before roles existed. Startup logs an
    error while that is true, and binding one principal switches enforcement on.
    """

    _as(monkeypatch, PORTAL)
    settings = _settings(auth_audience="https://mw.example.run.app")
    assert settings.auth_role_bindings == {}
    assert settings.role_bindings_missing

    for client in _client_with(settings):
        assert client.get("/agents", headers=HEADERS).status_code == 200
        assert (
            client.post(
                "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
            ).status_code
            == 201
        )
        # ...including the admin-only routes, because "not enforced" has to mean
        # not enforced. A partial grant would be a third behaviour nobody asked
        # for and nobody could reason about.
        assert client.delete("/agents/writer", headers=HEADERS).status_code == 200


def test_binding_one_principal_turns_enforcement_on_for_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The migration story: adding the first binding is the switch. No flag day,
    # and clearing the map rolls it back.
    _as(monkeypatch, "unbound@karoscmo.iam.gserviceaccount.com")
    for client in _client_with(_bound("editor")):
        assert (
            client.post(
                "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
            ).status_code
            == 403
        )


def test_the_default_role_is_configurable_for_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The documented escape hatch: widen the default while bindings are being
    # rolled out, then narrow it again. Explicit, and visible in the config.
    _as(monkeypatch, "someone-else@karoscmo.iam.gserviceaccount.com")
    settings = _bound("viewer", auth_default_role="editor")
    for client in _client_with(settings):
        assert (
            client.post(
                "/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS
            ).status_code
            == 201
        )


def test_disabling_auth_leaves_every_route_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # With authentication off there is no principal to bind a role to, so
    # enforcing roles would be a check that looks like security and is not.
    # This is also what makes the whole change inert until AUTH_ENABLED flips.
    for client in _client_with(_settings(auth_enabled=False)):
        assert (
            client.post("/agents", json={"slug": "writer", "name": "Writer"}).status_code == 201
        )
        assert client.delete("/agents/writer").status_code == 200


# --- The audit trail stops being self-reported -------------------------------


def test_the_prompt_author_is_the_verified_caller_not_the_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, PORTAL)
    for client in _client_with(_bound("editor")):
        client.post("/agents", json={"slug": "writer", "name": "Writer"}, headers=HEADERS)

        created = client.post(
            "/agents/writer/prompts",
            json={"content": "Be concise.", "created_by": "someone-else-entirely"},
            headers=HEADERS,
        )

        assert created.status_code == 201, created.text
        # `created_by` in the body is ignored rather than rejected: the portal
        # sends it today, and a 422 would break it for no safety gain. What
        # matters is that it is not believed.
        assert created.json()["created_by"] == PORTAL


# --- The wiring itself, not one route at a time ------------------------------


def _api_routes(app: FastAPI) -> list[Any]:
    """Every APIRoute in the app, reaching through included routers."""

    found: list[Any] = []

    def walk(routes: list[Any]) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(list(inner.routes))
            elif hasattr(route, "dependant") and hasattr(route, "methods"):
                found.append(route)

    walk(list(app.routes))
    return found


def _declared_minimum(route: Any) -> Role | None:
    """The highest role floor declared ON the route (not on its router)."""

    floors: list[Role] = []

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            call = getattr(sub, "call", None)
            if call is not None and getattr(call, "__qualname__", "").startswith("require_role"):
                for cell in getattr(call, "__closure__", None) or ():
                    if isinstance(cell.cell_contents, Role):
                        floors.append(cell.cell_contents)
            walk(sub)

    walk(route.dependant)
    return max(floors, key=lambda role: role.rank) if floors else None


def test_every_protected_router_carries_identity_and_a_read_floor() -> None:
    """A router added without both is a set of open routes nobody notices.

    Checked at the include, not per route: that is where the two are attached,
    and a new router is exactly the thing that gets added without them.
    """

    app = create_app()
    includes = [
        route.include_context
        for route in app.routes
        if type(route).__name__ == "_IncludedRouter"
    ]
    assert includes, "no included routers found — this test needs updating"

    open_routers, protected_routers = [], []
    for context in includes:
        names = {
            getattr(getattr(dep, "dependency", None), "__qualname__", "")
            for dep in context.dependencies
        }
        if not names:
            open_routers.append(context)
            continue
        assert "require_service_identity" in names
        assert any(name.startswith("require_role") for name in names)
        protected_routers.append(context)

    # Exactly one unauthenticated router: health. Cloud Run's probes carry no
    # identity token, and it exposes only reachability booleans.
    assert len(open_routers) == 1
    # Counted rather than merely non-zero, so ADDING a router is a decision
    # somebody makes here on purpose. It has already earned that: the clients
    # router (S-A15) failed this test on the way in, which is the moment to
    # confirm a new surface is authenticated rather than the moment after.
    #
    #   9th  the tenant-scoped run listing (S8)
    #  10th  the client re-seed control plane (S-A15)
    #  11th  the Configuration API (S4)
    assert len(protected_routers) == 11


def test_no_mutating_route_sits_at_the_read_floor() -> None:
    """Every POST/PUT/PATCH/DELETE names editor or admin explicitly.

    Without this, adding a write route and forgetting the dependency leaves it
    at the router-wide `viewer` floor — a write any reader can perform, and
    nothing about the route looks wrong.
    """

    # The one POST that changes nothing. It builds exactly what `/jobs` would
    # publish and then neither records a run nor sends it, so it is a read with
    # a request body -- and it exposes nothing `GET /context` does not already.
    # Listed by name, with the reason, because the alternative is loosening the
    # rule for every POST to accommodate one.
    READ_ONLY_POSTS = {"/agents/{agent_id}/payload"}

    app = create_app()
    unguarded = []
    for route in _api_routes(app):
        if str(route.path).startswith("/health") or str(route.path) in READ_ONLY_POSTS:
            continue
        methods = set(route.methods) - {"HEAD", "OPTIONS"}
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        minimum = _declared_minimum(route)
        if minimum is None or not minimum.satisfies(Role.EDITOR):
            unguarded.append(f"{sorted(methods)[0]} {route.path}")

    assert not unguarded, "mutating routes with no role floor above viewer: " + ", ".join(
        unguarded
    )


def test_destructive_routes_require_admin() -> None:
    """Removal and resurrection are admin, wherever they live.

    Named as a list rather than derived from the HTTP method, because DELETE is
    not the only way to remove something — `restore` and an agent's `status`
    are POST and PATCH, and they belong here for what they do.
    """

    expected_admin = {
        # Removal and resurrection of a record.
        "DELETE /agents/{agent_id}",
        "POST /agents/{agent_id}/restore",
        "DELETE /templates/{template_id}",
        "POST /templates/{template_id}/restore",
        "DELETE /agents/{agent_id}/examples/{example_id}",
        "DELETE /agents/{agent_id}/templates/{purpose}",
        # Whether an agent may run at all. Disabling one stops every client
        # that depends on it and nothing in the run path reports why.
        "PATCH /agents/{agent_id}/status",
        # The model catalog decides what every stage may run on and at what
        # price, so a row here reroutes or reprices work across all agents.
        "POST /models",
        "PATCH /models/{model_id}",
        # Repointing an alias changes what every stage naming it runs on, at
        # once, with no code change -- which is the point of an alias and the
        # reason it is admin. Lost when the S12/S4 merge conflict in this list
        # was resolved by keeping one side, which is what made CI red on
        # dev-shlomi.
        "PUT /models/aliases/{alias}",
        # Publishing and rolling back change what every client of an agent gets
        # on their next run, immediately, with no review step in between. That
        # is a different act from editing a draft (editor), and the ladder
        # should say so.
        "POST /config/agents/{agent_slug}/versions/{version}/publish",
        "POST /config/agents/{agent_slug}/rollback",
        "DELETE /config/agents/{agent_slug}/versions/{version}",
    }

    app = create_app()
    actual_admin = {
        f"{sorted(set(route.methods) - {'HEAD', 'OPTIONS'})[0]} {route.path}"
        for route in _api_routes(app)
        if _declared_minimum(route) is Role.ADMIN
    }

    assert actual_admin == expected_admin
