"""The prompt store agent-engine executes from.

What these pin is the difference between this and ``prompts.py``: an edit here
has to change what a run loads. The failure worth guarding is the one this
whole feature exists to fix -- a Studio that versions and promotes prompts
while the engine reads a different collection entirely and nothing anyone types
ever reaches an agent.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from tests.fake_firestore import FakeFirestoreClient


def publish(
    store: FakeFirestoreClient, settings: Settings, prompt_id: str, version: str, content: str
) -> None:
    """Seeds a prompt the way agent-engine's publish-prompts.ts does.

    Straight into the store rather than through an endpoint, because there is no
    create endpoint and that is deliberate: a prompt no stage's skillRef names
    is a prompt nothing loads, so offering the Studio a way to make one would be
    offering a control that does nothing.
    """
    prefix = settings.firestore_collection_prefix
    store.documents[f"{prefix}promptVersions/{prompt_id}@{version}"] = {"content": content}
    store.documents[f"{prefix}prompts/{prompt_id}"] = {"latestVersion": version}


def test_reads_the_text_a_stage_would_load(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    publish(fake_firestore_client, settings, "x-craft", "2", "You write short posts.")

    response = client.get("/engine-prompts/x-craft/versions/2")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == "You write short posts."
    assert body["skill_ref"] == "x-craft@2"
    assert body["pinned"] is True


def test_an_unpinned_read_follows_latest_version(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    # The engine's own reader falls back this way, so this one does too.
    publish(fake_firestore_client, settings, "x-craft", "2", "v2 text")

    body = client.get("/engine-prompts/x-craft").json()

    assert body["version"] == "2"
    assert body["pinned"] is False


def test_a_save_replaces_the_version_the_skillref_names(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    # In place, because a stage's skillRef pins one version: a new version
    # would be inert until someone changed TypeScript, which is exactly the
    # "nothing I edit takes effect" problem being fixed.
    publish(fake_firestore_client, settings, "x-craft", "2", "original")

    response = client.put("/engine-prompts/x-craft/versions/2", json={"content": "edited"})

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "edited"
    # And a fresh read — the engine's resolution path — sees it.
    assert client.get("/engine-prompts/x-craft/versions/2").json()["content"] == "edited"


def test_an_empty_save_is_refused(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    # The one edit that must not look like success. A stage with no system
    # prompt does not fail; it runs on its bare turn contract and produces
    # plausible unmoored output, which is worse than an error.
    publish(fake_firestore_client, settings, "x-craft", "2", "original")

    for blank in ["", "   \n  "]:
        response = client.put("/engine-prompts/x-craft/versions/2", json={"content": blank})
        assert response.status_code in (409, 422), response.text

    # The original survived every attempt.
    assert client.get("/engine-prompts/x-craft/versions/2").json()["content"] == "original"


def test_saving_keeps_the_superseded_text(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    # In-place editing trades away version history, so the text it replaces is
    # kept where the engine cannot see it: the engine reads only latestVersion
    # off the prompt document.
    publish(fake_firestore_client, settings, "x-craft", "2", "first")
    # `?actor=` is deliberately still sent here: it is what the portal sends
    # today, and the point of the assertion below is that the server no longer
    # believes it.
    client.put("/engine-prompts/x-craft/versions/2?actor=tomer", json={"content": "second"})
    client.put("/engine-prompts/x-craft/versions/2", json={"content": "third"})

    history = client.get("/engine-prompts/x-craft/history").json()

    assert [h["content"] for h in history] == ["first", "second"]
    # The actor is the VERIFIED caller, never the query string. This suite runs
    # with auth_enabled=False, so every caller is the anonymous identity -- and
    # a request that asked to be recorded as "tomer" is not.
    assert history[0]["replaced_by"] == "anonymous"
    assert history[1]["replaced_by"] == "anonymous"


def test_an_identical_save_does_not_add_history(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    publish(fake_firestore_client, settings, "x-craft", "2", "same")

    client.put("/engine-prompts/x-craft/versions/2", json={"content": "same"})

    assert client.get("/engine-prompts/x-craft/history").json() == []


def test_a_version_that_was_never_published_cannot_be_edited(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    # Writing one would create a prompt nothing loads. Silently doing nothing
    # is the failure this feature exists to fix, so it is an error instead.
    publish(fake_firestore_client, settings, "x-craft", "2", "exists")

    response = client.put("/engine-prompts/x-craft/versions/9", json={"content": "new"})

    assert response.status_code == 404, response.text


def test_an_unknown_prompt_reads_as_not_found(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    assert client.get("/engine-prompts/never-published/versions/1").status_code == 404
    assert client.get("/engine-prompts/never-published").status_code == 404


def test_history_is_empty_rather_than_missing_for_an_unedited_prompt(
    client: TestClient, fake_firestore_client: FakeFirestoreClient, settings: Settings
) -> None:
    publish(fake_firestore_client, settings, "x-craft", "2", "untouched")

    assert client.get("/engine-prompts/x-craft/history").json() == []
