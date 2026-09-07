"""Per-stage model selection: validation on the way in, delivery on the way out.

`kind` is `"agent"`, not `"ai"`, and that is the whole reason this feature did
not work when it first shipped: the engine calls a model step `"agent"` in its
own StepKindSchema and in engine_stages.json, this schema called it `"ai"`, and
so every seeded stage fell back to `"code"` and the Studio picker rendered on
none of them.

The two halves that matter are that a stage cannot name a model nothing routes,
and that a stage which names a real one actually reaches the engine with it.
A per-stage model that is accepted, stored, shown in the Studio and then
dropped from the wire message is the failure worth guarding: nothing surfaces
it, the run succeeds, and it succeeds on the wrong model at the wrong price.
"""

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import FakePublisherClient
from tests.test_models import make_model


def _stages(**overrides: Any) -> list[dict[str, Any]]:
    base: list[dict[str, Any]] = [
        {"id": "01-draft", "label": "Draft", "kind": "agent"},
        {"id": "02-verify", "label": "Verify", "kind": "code"},
    ]
    for stage in base:
        if stage["id"] in overrides:
            stage.update(overrides[stage["id"]])
    return base


def test_stage_can_name_a_model_from_the_catalog(client: TestClient, agent: dict[str, Any]) -> None:
    make_model(
        client,
        model_id="claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        provider_model_name="claude-haiku-4-5@20251001",
    )

    response = client.patch(
        f"/agents/{agent['id']}",
        json={"stages": _stages(**{"01-draft": {"model_id": "claude-haiku-4-5"}})},
    )

    assert response.status_code == 200, response.text
    stages = {s["id"]: s for s in response.json()["stages"]}
    assert stages["01-draft"]["model_id"] == "claude-haiku-4-5"
    assert stages["02-verify"]["model_id"] is None


def test_a_stage_naming_an_unknown_model_is_refused(
    client: TestClient, agent: dict[str, Any]
) -> None:
    # The reason `models` is a collection rather than free text. Caught on the
    # edit that introduced the typo, not as a tooling_error three layers away
    # at the model call.
    response = client.patch(
        f"/agents/{agent['id']}",
        json={"stages": _stages(**{"01-draft": {"model_id": "claude-sonnet-9-imaginary"}})},
    )

    assert response.status_code == 409, response.text
    assert "claude-sonnet-9-imaginary" in response.json()["detail"]


def test_editing_another_field_does_not_re_validate_untouched_stages(
    client: TestClient, agent: dict[str, Any]
) -> None:
    # A patch that never mentions stages must not be blocked by them.
    response = client.patch(f"/agents/{agent['id']}", json={"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


def test_dispatch_carries_only_the_stages_that_named_a_model(
    client: TestClient,
    agent: dict[str, Any],
    template: dict[str, Any],
    fake_publisher_client: FakePublisherClient,
) -> None:
    make_model(
        client,
        model_id="claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        provider_model_name="claude-haiku-4-5@20251001",
    )
    client.put(f"/agents/{agent['id']}/templates/primary", json={"template_ref": template["id"]})
    client.patch(
        f"/agents/{agent['id']}",
        json={"stages": _stages(**{"01-draft": {"model_id": "claude-haiku-4-5"}})},
    )

    response = client.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": "acme", "input": {}, "requested_by": "portal"},
    )
    assert response.status_code == 202, response.text

    _topic, data, _attributes = fake_publisher_client.published[0]
    body = json.loads(data)
    # camelCase, because this is the half of the message the engine's own
    # schema reads. The VALUE is the model's provider name, the id the engine's
    # own catalog knows, never the catalog document id it would refuse; a stage
    # seeded without an `agent_id` falls back to its step id as the key.
    assert body["stageModels"] == {"01-draft": "claude-haiku-4-5@20251001"}


def test_dispatch_keys_the_map_by_the_engine_agent_id_not_the_step_id(
    client: TestClient,
    agent: dict[str, Any],
    template: dict[str, Any],
    fake_publisher_client: FakePublisherClient,
) -> None:
    # `applyStageModelOverride` reads `stageModels[this.config.id]`: the agent
    # class's own id (`x-draft`), not the workflow step id (`10-draft-post`).
    # Keyed by the step id, a Studio pick was stored, displayed and ignored.
    make_model(
        client,
        model_id="claude-opus-4-8-on-vertex",
        display_name="Claude Opus 4.8",
        vendor="anthropic",
        provider_model_name="claude-opus-4-8",
    )
    client.put(f"/agents/{agent['id']}/templates/primary", json={"template_ref": template["id"]})
    response = client.patch(
        f"/agents/{agent['id']}",
        json={
            "stages": _stages(
                **{
                    "01-draft": {
                        "agent_id": "x-draft",
                        "default_model": "claude-sonnet-4-6",
                        "vendor": "anthropic",
                        "model_id": "claude-opus-4-8-on-vertex",
                    }
                }
            )
        },
    )
    assert response.status_code == 200, response.text
    stage = {s["id"]: s for s in response.json()["stages"]}["01-draft"]
    assert (stage["agent_id"], stage["default_model"], stage["vendor"]) == (
        "x-draft",
        "claude-sonnet-4-6",
        "anthropic",
    )

    client.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": "acme", "input": {}, "requested_by": "portal"},
    )
    _topic, data, _attributes = fake_publisher_client.published[0]
    assert json.loads(data)["stageModels"] == {"x-draft": "claude-opus-4-8"}


def test_a_stage_cannot_be_pointed_at_another_vendors_model(
    client: TestClient, agent: dict[str, Any]
) -> None:
    # The engine selects an adapter from the stage's compiled vendor alone and
    # refuses a cross-vendor override after the run has started. Refused here,
    # on the edit, with the same rule.
    make_model(client)  # gemini-2-5-pro, vendor google
    response = client.patch(
        f"/agents/{agent['id']}",
        json={
            "stages": _stages(
                **{"01-draft": {"vendor": "anthropic", "model_id": "gemini-2-5-pro"}}
            )
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "gemini-2-5-pro" in detail and "anthropic" in detail

    # A stage seeded before `vendor` existed is not blocked: nothing to compare.
    response = client.patch(
        f"/agents/{agent['id']}",
        json={"stages": _stages(**{"01-draft": {"model_id": "gemini-2-5-pro"}})},
    )
    assert response.status_code == 200, response.text


def test_dispatch_omits_the_map_entirely_when_no_stage_overrides(
    client: TestClient,
    agent: dict[str, Any],
    template: dict[str, Any],
    fake_publisher_client: FakePublisherClient,
) -> None:
    # Absent rather than empty: the engine already means "every stage keeps its
    # compiled default" when the key is missing, and an empty object would be a
    # second way to say the same thing.
    client.put(f"/agents/{agent['id']}/templates/primary", json={"template_ref": template["id"]})
    client.patch(f"/agents/{agent['id']}", json={"stages": _stages()})

    client.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": "acme", "input": {}, "requested_by": "portal"},
    )

    _topic, data, _attributes = fake_publisher_client.published[0]
    assert "stageModels" not in json.loads(data)
