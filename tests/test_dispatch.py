import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.db.firestore import RUNS, FirestoreDB
from tests.conftest import FakePublisherClient


def test_dispatch_publishes_payload_and_records_run(
    client: TestClient,
    agent: dict[str, Any],
    template: dict[str, Any],
    fake_publisher_client: FakePublisherClient,
) -> None:
    client.put(f"/agents/{agent['id']}/templates/primary", json={"template_ref": template["id"]})
    client.post(
        f"/agents/{agent['id']}/examples",
        json={"user_input": "topic: tea", "assistant_output": "Tea post"},
    )

    response = client.post(
        f"/agents/{agent['id']}/jobs",
        json={
            "client_slug": "acme",
            "job_type": "social_post",
            "input": {"topic": "cold brew"},
            "requested_by": "portal",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["topic"] == "projects/test-project/topics/test-jobs-topic"
    assert body["run"]["status"] == "dispatched"
    assert body["run"]["pubsub_message_id"] == body["pubsub_message_id"]
    assert body["run"]["prompt_version"] == 1
    assert body["run"]["job_type"] == "social_post"
    assert body["run"]["requested_by"] == "portal"

    topic, data, attributes = fake_publisher_client.published[0]
    assert topic == "projects/test-project/topics/test-jobs-topic"

    payload = json.loads(data.decode("utf-8"))
    assert payload["schema_version"] == 1
    assert payload["run_id"] == body["run"]["id"]
    assert payload["input"] == {"topic": "cold brew"}
    assert payload["agent"]["slug"] == agent["slug"]
    assert payload["system_prompt"]["version"] == 1
    assert payload["template"]["content"] == "<article>{{body}}</article>"
    assert [example["user_input"] for example in payload["few_shot_examples"]] == ["topic: tea"]

    assert attributes["run_id"] == body["run"]["id"]
    assert attributes["agent_slug"] == agent["slug"]
    assert attributes["job_type"] == "social_post"
    assert attributes["prompt_version"] == "1"
    assert attributes["template_version"] == "1"
    assert attributes["source"] == "agent-middleware"


def test_run_snapshot_references_versions_instead_of_copying_bodies(
    client: TestClient, agent: dict[str, Any], template: dict[str, Any]
) -> None:
    client.put(f"/agents/{agent['id']}/templates/primary", json={"template_ref": template["id"]})

    run = client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "input": {"topic": "matcha"}}
    ).json()["run"]

    assert run["input_payload"] == {
        "input": {"topic": "matcha"},
        "template_purpose": "primary",
        "example_count": 0,
        "template_id": template["id"],
        "template_version": 1,
    }
    assert run["template_version_id"] is not None


def test_caller_supplied_run_id_is_used(client: TestClient, agent: dict[str, Any]) -> None:
    response = client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_id": "portal-run-1"}
    )

    assert response.json()["run"]["id"] == "portal-run-1"
    assert client.get(f"/agents/{agent['id']}/runs/portal-run-1").status_code == 200


def test_duplicate_run_id_is_rejected(client: TestClient, agent: dict[str, Any]) -> None:
    client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_id": "portal-run-1"}
    )

    duplicate = client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_id": "portal-run-1"}
    )

    assert duplicate.status_code == 409


def test_caller_attributes_cannot_override_routing_attributes(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    client.post(
        f"/agents/{agent['id']}/jobs",
        json={
            "client_slug": "acme",
            "run_id": "run-x",
            "attributes": {
                "run_id": "spoofed",
                # The engine routes off these two; a caller must not be able to
                # aim a job at another tenant or workflow through free-form
                # attributes.
                "clientSlug": "someone-else",
                "productId": "not-this-one",
                "tenant": "acme",
            },
        },
    )

    _, _, attributes = fake_publisher_client.published[0]
    assert attributes["run_id"] == "run-x"
    assert attributes["clientSlug"] == "acme"
    assert attributes["productId"] == agent["slug"]
    # Anything that isn't a reserved routing key still passes through.
    assert attributes["tenant"] == "acme"


def test_dispatch_requires_an_active_prompt(client: TestClient) -> None:
    client.post("/agents", json={"slug": "bare", "name": "Bare"})

    response = client.post("/agents/bare/jobs", json={"client_slug": "acme"})

    assert response.status_code == 422
    assert "no active system prompt" in response.json()["detail"]


def test_dispatch_refuses_a_disabled_agent(client: TestClient, agent: dict[str, Any]) -> None:
    client.patch(f"/agents/{agent['id']}/status", json={"status": "disabled"})

    response = client.post(f"/agents/{agent['id']}/jobs", json={"client_slug": "acme"})

    assert response.status_code == 409


def test_payload_preview_publishes_nothing(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    response = client.post(
        f"/agents/{agent['id']}/payload",
        json={"client_slug": "acme", "input": {"topic": "preview"}},
    )

    assert response.status_code == 200
    assert response.json()["input"] == {"topic": "preview"}
    assert response.json()["run_id"] == "preview"
    assert fake_publisher_client.published == []
    assert client.get(f"/agents/{agent['id']}/runs").json()["items"] == []


def test_failed_publish_marks_the_run_failed(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    def explode(topic: str, data: bytes, **attributes: str) -> None:
        raise RuntimeError("pubsub unavailable")

    fake_publisher_client.publish = explode  # type: ignore[method-assign]

    response = client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_id": "doomed"}
    )

    assert response.status_code == 502
    run = client.get(f"/agents/{agent['id']}/runs/doomed").json()
    assert run["status"] == "failed"
    assert "pubsub unavailable" in run["error"]
    assert run["completed_at"] is not None


# --- Engine wire compatibility ------------------------------------------------
# agent-engine validates the message body against its own RunJobRequestSchema,
# which requires three camelCase keys at the top level and nacks anything
# without them. A regression here does not fail loudly: every dispatched job
# would retry five times and land in the dead-letter topic.


def test_published_message_carries_the_engine_routing_trio(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    response = client.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": "acme", "run_kind": "setup", "input": {"topic": "matcha"}},
    )
    assert response.status_code == 202, response.text

    _topic, data, _attributes = fake_publisher_client.published[0]
    body = json.loads(data)

    # Exactly what agent-engine's RunJobRequestSchema reads.
    assert body["clientSlug"] == "acme"
    assert body["productId"] == agent["slug"]
    assert body["runKind"] == "setup"


def test_run_kind_defaults_to_recurring(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    client.post(f"/agents/{agent['id']}/jobs", json={"client_slug": "acme"})

    _topic, data, _attributes = fake_publisher_client.published[0]
    assert json.loads(data)["runKind"] == "recurring"


def test_the_engine_trio_matches_the_richer_context_in_the_same_message(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    """Two shapes, one message — they must not disagree about the routing."""

    client.post(f"/agents/{agent['id']}/jobs", json={"client_slug": "acme"})

    _topic, data, _attributes = fake_publisher_client.published[0]
    body = json.loads(data)

    assert body["clientSlug"] == body["client_slug"]
    assert body["productId"] == body["product_id"] == body["agent"]["slug"]
    assert body["runKind"] == body["run_kind"]


def test_dispatch_without_a_client_slug_is_refused(
    client: TestClient, agent: dict[str, Any], fake_publisher_client: FakePublisherClient
) -> None:
    """Better a 422 than a job the engine cannot resolve a workspace for."""

    response = client.post(f"/agents/{agent['id']}/jobs", json={"input": {"topic": "x"}})

    assert response.status_code == 422
    assert fake_publisher_client.published == []


def test_an_unknown_run_kind_is_refused(client: TestClient, agent: dict[str, Any]) -> None:
    response = client.post(
        f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_kind": "backfill"}
    )

    assert response.status_code == 422


# --- client attribution (S8) ------------------------------------------------


def test_dispatch_records_the_tenant_on_the_run(
    client: TestClient, agent: dict[str, Any], template: dict[str, Any]
) -> None:
    """The whole of S8's first half.

    `client_slug` was already REQUIRED on the dispatch request -- the engine
    resolves a client's entire workspace from it -- and simply never reached the
    run document. So every run in the system was unattributable to the tenant
    that paid for it, while the value sat in the request that created it.
    """

    response = client.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": "geektime", "input": {"topic": "series A"}},
    )
    assert response.status_code == 202, response.text
    run = response.json()["run"]
    assert run["client_slug"] == "geektime"

    stored = client.get(f"/agents/{agent['id']}/runs/{run['id']}")
    assert stored.status_code == 200, stored.text
    assert stored.json()["client_slug"] == "geektime"


def test_a_registered_run_can_carry_a_tenant_but_is_not_forced_to(
    client: TestClient, agent: dict[str, Any]
) -> None:
    # The register path is for callers that publish their own payload. Requiring
    # the field would break them for no safety gain; omitting it costs
    # attribution, which the schema says in as many words.
    with_slug = client.post(
        f"/agents/{agent['id']}/runs", json={"client_slug": "geektime"}
    )
    without = client.post(f"/agents/{agent['id']}/runs", json={})

    assert with_slug.status_code == 201, with_slug.text
    assert without.status_code == 201, without.text
    assert with_slug.json()["client_slug"] == "geektime"
    assert without.json()["client_slug"] is None


def test_runs_are_listable_by_tenant_across_agents(
    client: TestClient, agent: dict[str, Any]
) -> None:
    """The query the stored field exists for.

    Storing `client_slug` without a tenant-scoped listing would be a column
    nobody can search, and "which runs did we do for this client" would still
    mean looping every agent.
    """

    other = client.post("/agents", json={"slug": "second-agent", "name": "Second"})
    assert other.status_code == 201, other.text

    for slug, agent_id in (
        ("geektime", agent["id"]),
        ("geektime", "second-agent"),
        ("someone-else", agent["id"]),
    ):
        created = client.post(f"/agents/{agent_id}/runs", json={"client_slug": slug})
        assert created.status_code == 201, created.text

    listed = client.get("/clients/geektime/runs")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["items"]) == 2
    assert {r["agent_id"] for r in body["items"]} == {agent["id"], "second-agent"}
    assert all(r["client_slug"] == "geektime" for r in body["items"])


def test_an_agent_listing_can_be_narrowed_to_one_tenant(
    client: TestClient, agent: dict[str, Any]
) -> None:
    for slug in ("geektime", "someone-else"):
        client.post(f"/agents/{agent['id']}/runs", json={"client_slug": slug})

    listed = client.get(f"/agents/{agent['id']}/runs?client_slug=geektime")

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["client_slug"] == "geektime"


async def test_a_run_from_before_this_field_reads_null_rather_than_failing(
    client: TestClient, agent: dict[str, Any], database: FirestoreDB
) -> None:
    """Every run document written before this change lacks the key entirely.

    The field is forward-only: `_run_snapshot` never carried a tenant, so there
    is nothing on this side to backfill from. A pre-S8 document therefore has to
    read as `null` rather than 500 the listing -- which would hide every working
    run behind it, the failure test_foreign_documents.py exists for.
    """

    now = datetime.now(UTC)
    await database.document(RUNS, "legacy-run").set(
        {
            "agent_id": agent["id"],
            "status": "dispatched",
            "job_type": None,
            "prompt_id": None,
            "prompt_version": None,
            "template_version_id": None,
            "input_payload": {},
            "output": None,
            "error": None,
            "pubsub_message_id": None,
            "requested_by": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    listed = client.get(f"/agents/{agent['id']}/runs")

    assert listed.status_code == 200, listed.text
    legacy = next(r for r in listed.json()["items"] if r["id"] == "legacy-run")
    assert legacy["client_slug"] is None
