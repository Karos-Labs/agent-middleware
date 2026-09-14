from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def run(client: TestClient, agent: dict[str, Any]) -> dict[str, Any]:
    """A dispatched run that has reported a successful result."""

    dispatched = client.post(
        f"/agents/{agent['id']}/jobs",
        json={
            "client_slug": "acme",
            "run_id": "run-1",
            "job_type": "social_post",
            "input": {"topic": "cold brew"},
        },
    )
    assert dispatched.status_code == 202, dispatched.text

    reported = client.patch(
        f"/agents/{agent['id']}/runs/run-1",
        json={"status": "succeeded", "output": {"content": "Cold brew is great."}},
    )
    assert reported.status_code == 200
    return reported.json()


def test_engine_result_completes_the_run(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    assert run["status"] == "succeeded"
    assert run["output"] == {"content": "Cold brew is great."}
    assert run["completed_at"] is not None


def test_submit_feedback_for_a_run(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    response = client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={
            "rating": 5,
            "status": "approved",
            "correction_notes": "shorten the opening",
            "reviewer": "shlomi",
            "tags": ["tone"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run_id"] == run["id"]
    assert body["agent_id"] == agent["id"]
    assert body["rating"] == 5
    assert body["status"] == "approved"
    assert body["promoted_example_id"] is None

    on_run = client.get(f"/agents/{agent['id']}/runs/{run['id']}/feedback").json()
    assert [item["id"] for item in on_run] == [body["id"]]

    detail = client.get(f"/agents/{agent['id']}/runs/{run['id']}").json()
    assert [item["id"] for item in detail["feedback"]] == [body["id"]]


def test_rating_is_bounded(client: TestClient, agent: dict[str, Any], run: dict[str, Any]) -> None:
    response = client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 6, "status": "approved"},
    )

    assert response.status_code == 422


def test_feedback_needs_a_registered_run(client: TestClient, agent: dict[str, Any]) -> None:
    response = client.post(
        f"/agents/{agent['id']}/runs/unknown-run/feedback",
        json={"rating": 4, "status": "approved"},
    )

    assert response.status_code == 404


def test_feedback_of_another_agent_is_not_reachable(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    client.post("/agents", json={"slug": "other", "name": "Other"})

    response = client.post(
        f"/agents/other/runs/{run['id']}/feedback", json={"rating": 4, "status": "approved"}
    )

    assert response.status_code == 404


def test_several_reviewers_can_rate_the_same_run(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    for rating, reviewer in ((5, "a"), (3, "b")):
        client.post(
            f"/agents/{agent['id']}/runs/{run['id']}/feedback",
            json={"rating": rating, "status": "approved", "reviewer": reviewer},
        )

    listed = client.get(f"/agents/{agent['id']}/feedback").json()
    assert [item["rating"] for item in listed["items"]] == [5, 3]

    filtered = client.get(f"/agents/{agent['id']}/feedback?min_rating=4").json()
    assert [item["reviewer"] for item in filtered["items"]] == ["a"]


def test_feedback_examples_use_the_correction_when_present(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={
            "rating": 5,
            "status": "approved",
            "correction_notes": "tighter",
            "corrected_output": "Cold brew, done right.",
            "reviewer": "shlomi",
        },
    )

    candidates = client.get(f"/agents/{agent['id']}/feedback/examples").json()["items"]

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["assistant_output"] == "Cold brew, done right."
    assert '"topic": "cold brew"' in candidate["user_input"]
    assert candidate["already_promoted"] is False


def test_feedback_examples_fall_back_to_the_run_output(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 4, "status": "approved"},
    )

    candidate = client.get(f"/agents/{agent['id']}/feedback/examples").json()["items"][0]

    assert candidate["assistant_output"] == "Cold brew is great."


def test_feedback_examples_exclude_poor_and_rejected_runs(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 2, "status": "rejected", "reviewer": "low"},
    )
    client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 5, "status": "needs_changes", "reviewer": "unapproved"},
    )

    assert client.get(f"/agents/{agent['id']}/feedback/examples").json()["items"] == []

    relaxed = client.get(
        f"/agents/{agent['id']}/feedback/examples?min_rating=1&status=rejected"
    ).json()
    assert [item["reviewer"] for item in relaxed["items"]] == ["low"]


def test_promote_feedback_creates_an_active_example(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    feedback = client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={
            "rating": 5,
            "status": "approved",
            "corrected_output": "Cold brew, done right.",
            "reviewer": "shlomi",
        },
    ).json()

    promoted = client.post(
        f"/agents/{agent['id']}/feedback/{feedback['id']}/promote", json={"tags": ["gold"]}
    )

    assert promoted.status_code == 201, promoted.text
    example = promoted.json()
    assert example["assistant_output"] == "Cold brew, done right."
    assert example["source"] == "feedback"
    assert example["source_run_id"] == run["id"]
    assert example["tags"] == ["gold"]
    assert example["is_active"] is True
    assert example["extra"]["feedback_id"] == feedback["id"]

    # The example now flows into every future job payload for this agent.
    context = client.get(f"/agents/{agent['id']}/context").json()
    assert [item["id"] for item in context["few_shot_examples"]] == [example["id"]]

    candidates = client.get(f"/agents/{agent['id']}/feedback/examples").json()["items"]
    assert candidates[0]["already_promoted"] is True


def test_promoting_twice_is_rejected(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    feedback = client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 5, "status": "approved", "corrected_output": "Better."},
    ).json()
    client.post(f"/agents/{agent['id']}/feedback/{feedback['id']}/promote", json={})

    again = client.post(f"/agents/{agent['id']}/feedback/{feedback['id']}/promote", json={})

    assert again.status_code == 409
    assert "already been promoted" in again.json()["detail"]


def test_promote_accepts_explicit_overrides(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    feedback = client.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 5, "status": "approved"},
    ).json()

    promoted = client.post(
        f"/agents/{agent['id']}/feedback/{feedback['id']}/promote",
        json={
            "user_input": "topic: cold brew",
            "assistant_output": "Hand-written ideal answer.",
            "label": "gold standard",
        },
    ).json()

    assert promoted["user_input"] == "topic: cold brew"
    assert promoted["assistant_output"] == "Hand-written ideal answer."
    assert promoted["label"] == "gold standard"


def test_registered_run_without_dispatch_can_receive_feedback(
    client: TestClient, agent: dict[str, Any]
) -> None:
    """The portal path: it publishes the payload itself, then registers the run."""

    registered = client.post(
        f"/agents/{agent['id']}/runs",
        json={
            "run_id": "portal-owned",
            "status": "dispatched",
            "prompt_version": 1,
            "input_payload": {"input": {"topic": "espresso"}},
            "requested_by": "portal",
        },
    )
    assert registered.status_code == 201, registered.text

    feedback = client.post(
        f"/agents/{agent['id']}/runs/portal-owned/feedback",
        json={"rating": 4, "status": "approved", "corrected_output": "Espresso, briefly."},
    )
    assert feedback.status_code == 201

    candidate = client.get(f"/agents/{agent['id']}/feedback/examples").json()["items"][0]
    assert candidate["run_id"] == "portal-owned"
    assert candidate["assistant_output"] == "Espresso, briefly."


def test_runs_can_be_listed_and_filtered_by_status(
    client: TestClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    client.post(f"/agents/{agent['id']}/jobs", json={"client_slug": "acme", "run_id": "run-2"})

    listed = client.get(f"/agents/{agent['id']}/runs").json()
    assert {item["id"] for item in listed["items"]} == {"run-1", "run-2"}
    assert listed["total"] is None
    assert listed["has_more"] is False

    succeeded = client.get(f"/agents/{agent['id']}/runs?status=succeeded").json()
    assert [item["id"] for item in succeeded["items"]] == ["run-1"]

    first_page = client.get(f"/agents/{agent['id']}/runs?limit=1").json()
    assert len(first_page["items"]) == 1
    assert first_page["has_more"] is True


def test_feedback_listing_survives_a_missing_composite_index(
    client: TestClient, monkeypatch: Any, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    """A listing that cannot be ORDERED should still list.

    The index for (agent_id, rating) is declared in firestore.indexes.json and
    was never deployed, so Firestore failed the whole query with
    FailedPrecondition and `GET /agents/{id}/feedback` returned 500 — taking
    the Studio page down with it. The index is the fix; this is the guard that
    stops the next undeployed index doing the same thing.
    """
    from google.api_core.exceptions import FailedPrecondition

    from app.services import feedback as feedback_module

    # Two verdicts, deliberately out of rating order, so the assertion below
    # can only pass if something sorted them.
    for rating in (2, 5):
        created = client.post(
            f"/agents/{agent['id']}/runs/{run['id']}/feedback",
            json={"rating": rating, "status": "approved", "reviewer": "shlomi"},
        )
        assert created.status_code == 201, created.text

    # S11 put the Firestore reads behind a store; the fallback under test is
    # the store's, so that is what gets the failing order_by.
    original = feedback_module.FirestoreFeedbackStore.list_for_agent

    async def raise_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Force the ordered path to fail the way an absent index fails.
        query_cls = type(self._db.collection("run_feedback"))
        real_order_by = query_cls.order_by

        def boom(*_a: Any, **_k: Any) -> Any:
            raise FailedPrecondition("400 The query requires an index.")

        query_cls.order_by = boom  # type: ignore[method-assign]
        try:
            return await original(self, *args, **kwargs)
        finally:
            query_cls.order_by = real_order_by  # type: ignore[method-assign]

    monkeypatch.setattr(feedback_module.FirestoreFeedbackStore, "list_for_agent", raise_once)

    response = client.get(f"/agents/{agent['id']}/feedback")

    assert response.status_code == 200, response.text
    # Proves the fallback actually ran rather than the test passing trivially:
    # the ordered query was replaced by one that raises, so any ordering in the
    # result can only have come from the in-process sort.
    ratings = [item["rating"] for item in response.json()["items"]]
    assert ratings == sorted(ratings, reverse=True)
    assert ratings == [5, 2], "the fallback should have sorted these itself"
