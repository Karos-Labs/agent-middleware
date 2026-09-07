"""Prices, aliases, and refusing to run on a model nothing can cost.

S12 / SCRUM-222. The bug these pin is not "a model is missing from a table" --
it is that both cost paths in the platform answer a missing model with Sonnet's
$3/$15 and no signal. `pricingForModel` in agent-engine falls back to
`DEFAULT_MODEL_PRICING`; `computeCostUsd` in karosCMO falls back to
`MODEL_PRICING._default`. Neither logs anything.

A plausible wrong number is the worst failure available in a cost report,
because nothing about it looks broken. So the catalog cannot hold an unpriced
row, an alias cannot point at one, and -- once enforcement is on -- a run
cannot be dispatched onto one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.firestore import MODELS, FirestoreDB
from app.main import build_services, create_app
from app.services.publisher import PublisherService

PRICED_MODEL: dict[str, Any] = {
    "model_id": "claude-sonnet-4-6-on-vertex",
    "display_name": "Claude Sonnet 4.6 (Vertex)",
    "vendor": "anthropic",
    "provider_model_name": "claude-sonnet-4-6",
    "region": "global",
    "input_per_1m": 3.0,
    "output_per_1m": 15.0,
    "cached_input_per_1m": 0.30,
    "pricing_checked_on": "2026-09-04",
    "pricing_source": "platform.claude.com/docs/en/about-claude/pricing",
}


def make_priced_model(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload = {**PRICED_MODEL, **overrides}
    response = client.post("/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- A model cannot exist without a price -----------------------------------


def test_a_model_with_no_price_is_refused(client: TestClient) -> None:
    """The row that caused the whole bug is now unrepresentable."""

    payload = {k: v for k, v in PRICED_MODEL.items() if not k.startswith(("input_", "output_"))}
    response = client.post("/models", json=payload)

    assert response.status_code == 422, response.text
    missing = {error["loc"][-1] for error in response.json()["detail"]}
    assert {"input_per_1m", "output_per_1m"} <= missing


def test_a_model_with_no_checked_date_is_refused(client: TestClient) -> None:
    # "We do not know how old this number is" is the state the hard-coded
    # tables are in: they carry no date, and two of their rows have been wrong
    # by 3x since the vendor cut its prices.
    payload = {k: v for k, v in PRICED_MODEL.items() if k != "pricing_checked_on"}
    assert client.post("/models", json=payload).status_code == 422


def test_a_cache_read_cannot_cost_more_than_a_fresh_read(client: TestClient) -> None:
    # A transcription error rather than a real price, and one that would make
    # every cached call look more expensive than not caching.
    response = client.post("/models", json={**PRICED_MODEL, "cached_input_per_1m": 4.0})
    assert response.status_code == 422
    assert "must not exceed input_per_1m" in response.text


def test_changing_a_price_must_restate_when_it_was_checked(client: TestClient) -> None:
    make_priced_model(client)

    stale = client.patch(
        "/models/claude-sonnet-4-6-on-vertex", json={"input_per_1m": 2.0}
    )
    assert stale.status_code == 422
    assert "pricing_checked_on" in stale.text

    dated = client.patch(
        "/models/claude-sonnet-4-6-on-vertex",
        json={"input_per_1m": 2.0, "pricing_checked_on": "2026-10-01"},
    )
    assert dated.status_code == 200, dated.text
    assert dated.json()["input_per_1m"] == 2.0


def test_the_cache_read_rate_defaults_to_a_tenth_of_input(client: TestClient) -> None:
    from app.api.schemas.model import ModelRead

    row = make_priced_model(
        client, model_id="gemini-2-5-pro", provider_model_name="gemini-2.5-pro",
        vendor="google", input_per_1m=1.25, output_per_1m=10.0,
        cached_input_per_1m=None,
    )
    parsed = ModelRead.model_validate(row)
    assert parsed.cache_read_rate == pytest.approx(0.125)

    explicit = ModelRead.model_validate({**row, "cached_input_per_1m": 0.31})
    assert explicit.cache_read_rate == 0.31


# --- Two vendor axes --------------------------------------------------------


def test_route_is_derived_from_vendor_when_unstated(client: TestClient) -> None:
    """`vendor` and `route` answer different questions (C6 §9.2).

    agent-middleware's `vendor` is who makes the model; agent-engine's own
    vendor axis is how this deployment reaches it. Llama on Model Garden is
    `vendor: meta, route: model-garden`.
    """

    google = make_priced_model(
        client, model_id="gemini-2-5-flash", vendor="google",
        provider_model_name="gemini-2.5-flash", input_per_1m=0.3, output_per_1m=2.5,
        cached_input_per_1m=None,
    )
    assert google["route"] == "gemini"

    meta = make_priced_model(
        client, model_id="llama-3-3-70b-instruct-maas", vendor="meta",
        provider_model_name="meta/llama-3.3-70b-instruct-maas",
        input_per_1m=0.72, output_per_1m=0.72, cached_input_per_1m=None,
    )
    assert meta["route"] == "model-garden"


def test_an_explicit_route_wins_over_the_derivation(client: TestClient) -> None:
    # A deployment fronting Claude through an OpenAI-shaped gateway.
    row = make_priced_model(client, route="openai-compatible")
    assert row["route"] == "openai-compatible"
    assert row["vendor"] == "anthropic"


# --- Pricing lookup ---------------------------------------------------------


async def test_pricing_for_a_legacy_row_raises_instead_of_guessing(
    client: TestClient, database: FirestoreDB
) -> None:
    """The one behaviour the ticket asks for, at the lookup itself.

    A row written before S12 has no price. Answering that with a default is
    what produced three years of wrong Opus numbers, so it raises.
    """

    from app.core.exceptions import ResourceNotFoundError
    from app.services.models import ModelService

    await database.document(MODELS, "claude-opus-4-8-on-vertex").set(
        {
            "id": "claude-opus-4-8-on-vertex",
            "model_id": "claude-opus-4-8-on-vertex",
            "display_name": "Claude Opus 4.8 (Vertex)",
            "vendor": "anthropic",
            "availability": "not_enabled",
            "provider_model_name": "claude-opus-4-8",
            "supports_tools": True,
            "tiers": ["pinned"],
        }
    )
    service = ModelService(database)

    with pytest.raises(ResourceNotFoundError) as raised:
        await service.pricing_for("claude-opus-4-8-on-vertex")
    assert "claude-opus-4-8-on-vertex" in str(raised.value)


async def test_pricing_for_a_priced_row_returns_its_provenance(
    client: TestClient, database: FirestoreDB
) -> None:
    from app.services.models import ModelService

    make_priced_model(client)
    pricing = await ModelService(database).pricing_for("claude-sonnet-4-6-on-vertex")

    assert pricing["input_per_1m"] == 3.0
    assert pricing["output_per_1m"] == 15.0
    # Not decoration: without these two, a wrong number has no paper trail.
    assert pricing["pricing_checked_on"] == "2026-09-04"
    assert "platform.claude.com" in pricing["pricing_source"]


# --- Aliases ----------------------------------------------------------------


def test_an_alias_resolves_to_the_provider_string_the_router_needs(
    client: TestClient,
) -> None:
    make_priced_model(client)

    created = client.put(
        "/models/aliases/sonnet",
        json={"model_id": "claude-sonnet-4-6-on-vertex", "provider_policy": "pinned"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["provider_model_name"] == "claude-sonnet-4-6"
    assert created.json()["region"] == "global"


def test_repointing_an_alias_is_the_operation_not_a_delete_and_recreate(
    client: TestClient,
) -> None:
    """A new model generation should be this call and nothing else.

    If repointing needed a delete first there would be a window where the alias
    does not resolve -- during exactly the change everyone performs.
    """

    make_priced_model(client)
    make_priced_model(
        client, model_id="claude-sonnet-5-on-vertex",
        provider_model_name="claude-sonnet-5", input_per_1m=2.0, output_per_1m=10.0,
        cached_input_per_1m=0.20,
    )

    first = client.put(
        "/models/aliases/sonnet",
        json={"model_id": "claude-sonnet-4-6-on-vertex", "provider_policy": "pinned"},
    ).json()
    second = client.put(
        "/models/aliases/sonnet",
        json={"model_id": "claude-sonnet-5-on-vertex", "provider_policy": "pinned"},
    )

    assert second.status_code == 200, second.text
    assert second.json()["model_id"] == "claude-sonnet-5-on-vertex"
    assert second.json()["provider_model_name"] == "claude-sonnet-5"
    # The alias is the same object, so its history is not reset by a repoint.
    assert second.json()["created_at"] == first["created_at"]
    assert len(client.get("/models/aliases").json()) == 1


async def test_an_alias_cannot_point_at_a_model_with_no_price(
    client: TestClient, database: FirestoreDB
) -> None:
    # Written straight to the store, because the API refuses to create it --
    # which is the point: the only way this row exists is that it predates S12.
    await database.document(MODELS, "mystery-model").set(
        {
            "id": "mystery-model",
            "model_id": "mystery-model",
            "display_name": "Mystery",
            "vendor": "other",
            "availability": "available",
            "provider_model_name": "mystery",
            "supports_tools": True,
            "tiers": [],
        }
    )

    refused = client.put(
        "/models/aliases/mystery",
        json={"model_id": "mystery-model", "provider_policy": "commodity"},
    )
    assert refused.status_code == 409
    assert "nothing can cost" in refused.text


def test_an_alias_to_an_unknown_model_is_404(client: TestClient) -> None:
    assert client.put(
        "/models/aliases/ghost",
        json={"model_id": "not-in-the-catalog", "provider_policy": "pinned"},
    ).status_code == 404


def test_aliases_are_listed_before_the_model_id_route_can_swallow_them(
    client: TestClient,
) -> None:
    """`GET /models/aliases` must not resolve as "the model called aliases".

    FastAPI matches in declaration order, so a literal path declared after a
    path parameter is unreachable. This is the test that catches someone moving
    the alias routes below `/{model_id}`.
    """

    assert client.get("/models/aliases").status_code == 200
    assert client.get("/models/pricing-coverage").status_code == 200


# --- Coverage ---------------------------------------------------------------


def test_coverage_names_the_agent_and_stage_behind_every_gap(
    client: TestClient, agent: dict[str, Any]
) -> None:
    """The pre-flight before enforcement is switched on.

    The `agent` fixture names `claude-opus-5` as its default model and nothing
    has catalogued it, which is exactly the shape of the real gap.
    """

    coverage = client.get("/models/pricing-coverage")
    assert coverage.status_code == 200, coverage.text
    body = coverage.json()

    assert "claude-opus-5" in body["referenced_models"]
    gap = next(g for g in body["gaps"] if g["model_id"] == "claude-opus-5")
    assert gap["reason"] == "missing"
    assert gap["agent_slug"] == agent["slug"]
    assert gap["stage_id"] is None  # the agent's own default, not a stage


async def test_coverage_tells_a_missing_row_apart_from_an_unpriced_one(
    client: TestClient, agent: dict[str, Any], database: FirestoreDB
) -> None:
    # The two need different fixes: one is "catalog it", the other is "someone
    # catalogued it before prices existed and never came back".
    await database.document(MODELS, "claude-opus-5").set(
        {
            "id": "claude-opus-5",
            "model_id": "claude-opus-5",
            "display_name": "Claude Opus 5",
            "vendor": "anthropic",
            "availability": "available",
            "provider_model_name": "claude-opus-5",
            "supports_tools": True,
            "tiers": ["pinned"],
        }
    )

    gaps = client.get("/models/pricing-coverage").json()["gaps"]
    assert next(g for g in gaps if g["model_id"] == "claude-opus-5")["reason"] == "unpriced"


def test_coverage_is_empty_once_every_referenced_model_is_priced(
    client: TestClient, agent: dict[str, Any]
) -> None:
    make_priced_model(
        client, model_id="claude-opus-5", provider_model_name="claude-opus-5",
        input_per_1m=5.0, output_per_1m=25.0, cached_input_per_1m=0.50,
    )
    body = client.get("/models/pricing-coverage").json()
    assert body["gaps"] == []
    assert "claude-opus-5" in body["priced_models"]


# --- The dispatch guard -----------------------------------------------------


@pytest.fixture
def enforcing_client(
    database: FirestoreDB, publisher_service: PublisherService
) -> Iterator[TestClient]:
    """A client with MODEL_PRICING_ENFORCED=true.

    Its own app rather than a mutated fixture, because the setting is read once
    when the services are built -- which is also how it behaves in production.
    """

    settings = Settings(
        gcp_project_id="test-project",
        pubsub_job_topic_id="test-jobs-topic",
        auth_enabled=False,
        model_pricing_enforced=True,
    )
    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        build_services(app, settings, database, publisher=publisher_service)
        yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as test_client:
        yield test_client


def _agent_with_a_prompt(client: TestClient, model: str) -> dict[str, Any]:
    created = client.post(
        "/agents",
        json={"slug": "priced-agent", "name": "Priced", "model": model},
    )
    assert created.status_code == 201, created.text
    prompt = client.post(
        f"/agents/{created.json()['id']}/prompts", json={"content": "Draft a post."}
    )
    assert prompt.status_code == 201, prompt.text
    return created.json()


def test_enforcement_refuses_a_dispatch_onto_an_unpriceable_model(
    enforcing_client: TestClient,
) -> None:
    created = _agent_with_a_prompt(enforcing_client, "claude-opus-5")

    response = enforcing_client.post(
        f"/agents/{created['id']}/jobs",
        json={"client_slug": "geektime", "input": {"topic": "series A"}},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "claude-opus-5" in detail
    assert "agent default" in detail
    # The message has to say what to do about it, not just that it happened.
    assert "pricing-coverage" in detail


def test_a_refused_dispatch_leaves_no_run_behind(enforcing_client: TestClient) -> None:
    """Checked before the run document is written, deliberately.

    A failed run someone has to explain is worse than no run: it appears in the
    client's history as an attempt that went wrong, when nothing was attempted.
    """

    created = _agent_with_a_prompt(enforcing_client, "claude-opus-5")
    enforcing_client.post(
        f"/agents/{created['id']}/jobs", json={"client_slug": "geektime", "input": {}}
    )

    runs = enforcing_client.get(f"/agents/{created['id']}/runs")
    assert runs.status_code == 200, runs.text
    assert runs.json()["items"] == []


def test_enforcement_allows_a_dispatch_onto_a_priced_model(
    enforcing_client: TestClient,
) -> None:
    make_priced_model(
        enforcing_client, model_id="claude-opus-5", provider_model_name="claude-opus-5",
        input_per_1m=5.0, output_per_1m=25.0, cached_input_per_1m=0.50,
    )
    created = _agent_with_a_prompt(enforcing_client, "claude-opus-5")

    response = enforcing_client.post(
        f"/agents/{created['id']}/jobs",
        json={"client_slug": "geektime", "input": {"topic": "series A"}},
    )
    assert response.status_code == 202, response.text


def test_the_preview_refuses_what_the_dispatch_would_refuse(
    enforcing_client: TestClient,
) -> None:
    # A preview whose whole job is to show what WOULD be published should not
    # show a payload that would be refused.
    created = _agent_with_a_prompt(enforcing_client, "claude-opus-5")
    preview = enforcing_client.post(
        f"/agents/{created['id']}/payload",
        json={"client_slug": "geektime", "input": {}},
    )
    assert preview.status_code == 422, preview.text


def test_with_enforcement_off_the_dispatch_proceeds(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Default-off, because turning it on before the catalog is seeded turns
    every dispatch into a 422 and the way that gets noticed is a client asking
    why nothing ran.

    Off is not silent, though: the gap is logged with the agent that names it.
    """

    created = _agent_with_a_prompt(client, "claude-opus-5")

    with caplog.at_level("WARNING"):
        response = client.post(
            f"/agents/{created['id']}/jobs",
            json={"client_slug": "geektime", "input": {}},
        )

    assert response.status_code == 202, response.text
    assert any("unpriceable model reference" in record.message for record in caplog.records)


# --- The seeded numbers themselves ------------------------------------------


def test_the_seeded_catalog_carries_the_corrected_prices() -> None:
    """A regression guard on the numbers, not on the mechanism.

    Both hard-coded tables price Opus at $15/$75. The published price is $5/$25,
    so every Opus step in every cost report the platform has produced is
    overstated threefold. This test is what fails if someone "restores" the old
    figures from the table they are more familiar with.
    """

    from scripts.seed_models import CATALOG

    by_id = {entry["model_id"]: entry for entry in CATALOG}

    for model_id in ("claude-opus-4-8-on-vertex", "claude-opus-5-on-vertex"):
        assert by_id[model_id]["input_per_1m"] == 5.0, "Opus is $5/1M in, not $15"
        assert by_id[model_id]["output_per_1m"] == 25.0, "Opus is $25/1M out, not $75"

    haiku = by_id["claude-haiku-4-5-on-vertex"]
    assert (haiku["input_per_1m"], haiku["output_per_1m"]) == (1.0, 5.0)

    # The tertiary fallback, which nothing could price at all.
    fallback = by_id["gemini-1-5-flash"]
    assert fallback["input_per_1m"] == 0.075
    assert "PER 1,000 CHARACTERS" in fallback["pricing_source"]

    # Every row states where its number came from and carries a checked date.
    for entry in CATALOG:
        assert entry.get("pricing_source")
        assert entry["output_per_1m"] >= entry["input_per_1m"]


def test_the_seed_refuses_a_swapped_price_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mistake this file is most likely to make.

    A digit dropped from an output price, or the pair transposed, is invisible
    in review and wrong in every report afterwards. The seed checks before it
    writes.
    """

    from scripts import seed_models

    swapped = (
        {
            **seed_models.CATALOG[0],
            "input_per_1m": 15.0,
            "output_per_1m": 3.0,
        },
    )
    monkeypatch.setattr(seed_models, "CATALOG", swapped)
    monkeypatch.setattr(seed_models, "ALIASES", ())

    problems = seed_models._check_prices_are_sane()
    assert any("probably swapped" in problem for problem in problems)


def test_the_seed_refuses_an_alias_pointing_outside_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import seed_models

    monkeypatch.setattr(
        seed_models,
        "ALIASES",
        ({"alias": "ghost", "model_id": "not-catalogued", "provider_policy": "pinned"},),
    )
    problems = seed_models._check_prices_are_sane()
    assert any("not in the catalog" in problem for problem in problems)
