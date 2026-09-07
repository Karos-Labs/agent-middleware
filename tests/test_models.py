"""The normalized model catalog.

What these pin is mostly about `not_enabled`: a model Vertex offers that this
deployment does not route stays visible, stays unselectable, and can be asked
for. Hiding it would make the catalog read as "this is everything Vertex has",
which is how someone concludes a model is unavailable when it is one config
change away.
"""

from typing import Any

from fastapi.testclient import TestClient


def make_model(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_id": "gemini-2-5-pro",
        "display_name": "Gemini 2.5 Pro",
        "vendor": "google",
        "provider_model_name": "gemini-2.5-pro",
        "region": "us-central1",
        "tiers": ["pinned", "portable"],
        # Required as of S12: a model with no price is the row that made every
        # cost path fall through to Sonnet's $3/$15 without saying so.
        "input_per_1m": 1.25,
        "output_per_1m": 10.0,
        "pricing_checked_on": "2026-09-04",
    }
    payload.update(overrides)
    response = client.post("/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_model_id_is_the_document_id(client: TestClient) -> None:
    # What makes an agent stage's stored modelId a reference rather than a
    # spelling: it either resolves to a document or it does not.
    created = make_model(client)
    assert created["id"] == created["model_id"] == "gemini-2-5-pro"
    assert client.get("/models/gemini-2-5-pro").status_code == 200


def test_registering_the_same_model_twice_conflicts(client: TestClient) -> None:
    make_model(client)
    assert client.post(
        "/models",
        json={
            "model_id": "gemini-2-5-pro",
            "display_name": "Duplicate",
            "vendor": "google",
            "provider_model_name": "gemini-2.5-pro",
            "input_per_1m": 1.25,
            "output_per_1m": 10.0,
            "pricing_checked_on": "2026-09-04",
        },
    ).status_code == 409


def test_provider_name_is_separate_from_the_id(client: TestClient) -> None:
    # Claude on Vertex is published under a different string from Claude on
    # Anthropic's own API. The engine's router needs the one it will send, and
    # the catalog needs a stable id that does not change when a vendor renames
    # its endpoint.
    created = make_model(
        client,
        model_id="claude-sonnet-4-6-on-vertex",
        display_name="Claude Sonnet 4.6 (Vertex)",
        vendor="anthropic",
        provider_model_name="claude-sonnet-4-6@20260219",
    )
    assert created["model_id"] != created["provider_model_name"]


def test_new_models_default_to_available(client: TestClient) -> None:
    assert make_model(client)["availability"] == "available"


def test_not_enabled_models_are_listed_not_hidden(client: TestClient) -> None:
    make_model(client)
    make_model(
        client,
        model_id="gemini-3-ultra",
        display_name="Gemini 3 Ultra",
        availability="not_enabled",
    )

    ids = [m["model_id"] for m in client.get("/models").json()["items"]]
    assert "gemini-3-ultra" in ids, "a model this deployment cannot route must still be visible"


def test_available_models_sort_before_unselectable_ones(client: TestClient) -> None:
    # The dropdown reads this order directly; one that reorders itself between
    # loads is its own bug.
    make_model(client, model_id="zzz-available", display_name="Zzz Available")
    make_model(
        client,
        model_id="aaa-not-enabled",
        display_name="Aaa Not Enabled",
        availability="not_enabled",
    )
    make_model(client, model_id="mmm-retired", display_name="Mmm Retired", availability="retired")

    states = [m["availability"] for m in client.get("/models").json()["items"]]
    assert states == ["available", "not_enabled", "retired"]


def test_can_filter_to_selectable_models(client: TestClient) -> None:
    make_model(client)
    make_model(client, model_id="gemini-3-ultra", display_name="G3", availability="not_enabled")

    items = client.get("/models", params={"availability": "available"}).json()["items"]
    assert [m["model_id"] for m in items] == ["gemini-2-5-pro"]


def test_retiring_a_model_keeps_it_resolvable(client: TestClient) -> None:
    # A stage may already reference it, and an old run's recorded model must
    # still resolve to something that explains what it was.
    make_model(client)
    patched = client.patch("/models/gemini-2-5-pro", json={"availability": "retired"})
    assert patched.status_code == 200
    assert patched.json()["availability"] == "retired"
    assert client.get("/models/gemini-2-5-pro").status_code == 200


def test_patch_leaves_unset_fields_alone(client: TestClient) -> None:
    make_model(client, description="original")
    client.patch("/models/gemini-2-5-pro", json={"display_name": "Renamed"})

    row = client.get("/models/gemini-2-5-pro").json()
    assert row["display_name"] == "Renamed"
    assert row["description"] == "original"


def test_access_request_records_the_ask_without_enabling_anything(client: TestClient) -> None:
    # Enabling a model means the engine has to route it and someone has to
    # accept its cost. This captures who asked; a human does the enabling.
    make_model(client, model_id="gemini-3-ultra", display_name="G3", availability="not_enabled")

    response = client.post(
        "/models/gemini-3-ultra/access-request",
        json={
            "requested_by": "tomer@karoslabs.com",
            "reason": "better long-context reasoning",
            "agent_id": "x-agent",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "open"

    # Still not selectable.
    assert client.get("/models/gemini-3-ultra").json()["availability"] == "not_enabled"


def test_access_request_for_an_unknown_model_is_404(client: TestClient) -> None:
    assert client.post(
        "/models/does-not-exist/access-request",
        json={"requested_by": "someone@example.com"},
    ).status_code == 404


def test_model_id_charset_is_enforced(client: TestClient) -> None:
    # The id goes in a URL path and is a Firestore document id.
    assert client.post(
        "/models",
        json={
            "model_id": "Gemini 2.5 Pro",
            "display_name": "Bad Id",
            "vendor": "google",
            "provider_model_name": "gemini-2.5-pro",
        },
    ).status_code == 422
