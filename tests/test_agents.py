from typing import Any

from fastapi.testclient import TestClient


def test_create_agent_returns_slug_as_id(client: TestClient) -> None:
    response = client.post("/agents", json={"slug": "writer", "name": "Writer"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "writer"
    assert body["slug"] == "writer"
    assert body["status"] == "active"
    assert body["deleted_at"] is None


def test_duplicate_slug_is_rejected(client: TestClient) -> None:
    client.post("/agents", json={"slug": "writer", "name": "Writer"})

    duplicate = client.post("/agents", json={"slug": "writer", "name": "Other"})

    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_invalid_slug_is_rejected(client: TestClient) -> None:
    response = client.post("/agents", json={"slug": "Not A Slug", "name": "Writer"})

    assert response.status_code == 422


def test_get_unknown_agent_returns_404(client: TestClient) -> None:
    assert client.get("/agents/nope").status_code == 404


def test_update_agent_patches_only_given_fields(
    client: TestClient, agent: dict[str, Any]
) -> None:
    response = client.patch(
        f"/agents/{agent['id']}", json={"model_params": {"temperature": 0.9}}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_params"] == {"temperature": 0.9}
    assert body["name"] == agent["name"]
    assert body["model"] == agent["model"]


def test_disable_and_reenable_agent(client: TestClient, agent: dict[str, Any]) -> None:
    disabled = client.patch(f"/agents/{agent['id']}/status", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    enabled = client.patch(f"/agents/{agent['id']}/status", json={"status": "active"})
    assert enabled.json()["status"] == "active"


def test_logical_delete_hides_agent_but_keeps_document(
    client: TestClient, agent: dict[str, Any]
) -> None:
    deleted = client.delete(f"/agents/{agent['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None
    assert deleted.json()["status"] == "disabled"

    assert client.get(f"/agents/{agent['id']}").status_code == 404
    assert client.get(f"/agents/{agent['id']}?include_deleted=true").status_code == 200

    restored = client.post(f"/agents/{agent['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None


def test_list_agents_filters_and_excludes_deleted(client: TestClient) -> None:
    client.post("/agents", json={"slug": "a-writer", "name": "Alpha", "tags": ["content"]})
    client.post("/agents", json={"slug": "b-builder", "name": "Beta", "agent_type": "pages"})
    client.delete("/agents/b-builder")

    listed = client.get("/agents").json()
    assert [item["id"] for item in listed["items"]] == ["a-writer"]
    assert listed["total"] == 1
    assert listed["has_more"] is False

    with_deleted = client.get("/agents?include_deleted=true").json()
    assert with_deleted["total"] == 2

    by_tag = client.get("/agents?tag=content").json()
    assert [item["id"] for item in by_tag["items"]] == ["a-writer"]

    by_query = client.get("/agents?q=beta&include_deleted=true").json()
    assert [item["id"] for item in by_query["items"]] == ["b-builder"]

    by_type = client.get("/agents?agent_type=pages&include_deleted=true").json()
    assert [item["id"] for item in by_type["items"]] == ["b-builder"]


def test_list_agents_paginates(client: TestClient) -> None:
    for index in range(3):
        client.post("/agents", json={"slug": f"agent-{index}", "name": f"Agent {index}"})

    first = client.get("/agents?limit=2").json()
    assert len(first["items"]) == 2
    assert first["total"] == 3
    assert first["has_more"] is True

    second = client.get("/agents?limit=2&offset=2").json()
    assert len(second["items"]) == 1
    assert second["has_more"] is False


# --- POST /agents must persist everything AgentCreate accepts ---------------
#
# The bug these guard: `AgentService.create` built its document from eight
# fields and dropped the other seven on the floor. They were only ever
# persisted by PATCH or by the seeder, so an agent created through the API came
# out of the catalog with no icon, no category, no price, no inputs and no
# stages -- and, because AgentRead supplies defaults for every one of them, the
# 201 response looked plausible while the stored record was missing half of it.


def _full_agent_payload() -> dict[str, Any]:
    """Every field ``AgentCreate`` accepts, each set to a non-default value.

    Non-default matters: a field left at its default cannot distinguish
    "persisted correctly" from "dropped and re-defaulted on the way out", which
    is exactly how this bug stayed invisible.
    """

    return {
        "slug": "full-agent",
        "name": "Full Agent",
        "description": "Every field populated",
        "status": "disabled",
        "agent_type": "post_writer",
        "model": "claude-opus-5",
        "model_params": {"temperature": 0.4},
        "config": {"lane": "founder"},
        "tags": ["content", "social"],
        "icon": "Camera",
        "category": "social",
        "credit_cost": 12,
        "is_public": False,
        "required_inputs": [
            {
                "key": "requestedLane",
                "type": "select",
                "label": "Lane",
                "help_text": "Which lane to draft for",
                "required": True,
                "placeholder": "founder",
                "options": ["founder", "company"],
            }
        ],
        "stages": [
            {
                "id": "01-draft",
                "label": "Draft",
                "description": "Write the post",
                # A gate stage, so is_gate and gate_kind are both non-default
                # too: a stage recorded as ordinary code when it actually waits
                # for a person is the bug the generator fix removes, and this
                # is the round trip that proves the fields survive a create.
                "is_gate": True,
                "gate_kind": "batch_review",
                "kind": "gate",
                "skill_ref": "x-draft@3",
                "model_id": None,
            }
        ],
        "stages_read_only": False,
        # --- C4 descriptor fields ---
        "capabilities": ["draft_social_post"],
        "platforms": ["x"],
        "consumes_media": True,
        "supports_target_date": True,
        "custom_agent_keys": ["karos-x-agent-v2"],
        "gates": ["batch_review"],
        "readiness": {
            "hard": ["client/profile", "client/config:xHandle"],
            "soft": ["topics/catalog"],
        },
    }


def test_the_full_payload_covers_every_field_the_schema_accepts() -> None:
    """The guard that stops this bug coming back for agent number sixteen.

    Adding a field to ``AgentCreate`` without adding it here fails this test,
    and the round-trip test below is what then forces ``create`` to persist it.
    Without this assertion the round-trip test silently stops covering new
    fields the moment one is added.
    """

    from app.api.schemas.agent import AgentCreate

    assert set(_full_agent_payload()) == set(AgentCreate.model_fields)


def test_create_persists_every_field_rather_than_echoing_defaults(
    client: TestClient,
) -> None:
    payload = _full_agent_payload()

    created = client.post("/agents", json=payload)
    assert created.status_code == 201, created.text

    # Read it back rather than trusting the 201 body: the response is built
    # from what create() returned, and the whole failure was that the returned
    # dict and the stored document were not the same thing.
    stored = client.get(f"/agents/{payload['slug']}")
    assert stored.status_code == 200, stored.text
    body = stored.json()

    for field, expected in payload.items():
        assert body[field] == expected, f"{field} was not persisted"


def test_create_refuses_a_stage_naming_a_model_the_catalog_does_not_have(
    client: TestClient,
) -> None:
    # PATCH has refused this since the models collection existed; create could
    # not, because it never looked at stages at all. An agent could therefore
    # be born pointing at a model nothing routes, and only a later unrelated
    # edit would surface it.
    payload = _full_agent_payload()
    payload["stages"][0]["model_id"] = "claude-sonnet-9-imaginary"

    response = client.post("/agents", json=payload)

    assert response.status_code == 409, response.text
    assert "claude-sonnet-9-imaginary" in response.json()["detail"]
    # ...and nothing was written: a rejected create must not leave a partial row.
    assert client.get(f"/agents/{payload['slug']}").status_code == 404


def test_create_accepts_a_stage_naming_a_model_that_exists(client: TestClient) -> None:
    from tests.test_models import make_model

    make_model(
        client,
        model_id="claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        provider_model_name="claude-haiku-4-5@20251001",
    )

    payload = _full_agent_payload()
    payload["stages"][0]["model_id"] = "claude-haiku-4-5"

    response = client.post("/agents", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["stages"][0]["model_id"] == "claude-haiku-4-5"
