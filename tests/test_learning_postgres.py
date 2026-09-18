"""The learning loop on Postgres (C7 / SCRUM-461, 462, 463).

Against a real PostgreSQL 16 with migration 0007 applied, and the in-memory
workspace store standing in for the bucket. What these pin:

    * PROJECT writes the C7 §2 files in the C7 §2.0 envelope, and only the
      files that mean something: windows are written empty, stores are left
      absent until they hold something. A second pass with nothing changed
      writes nothing and leaves `projectedAt` alone.
    * DISPATCH of a platform agent projects before it publishes; dispatch of
      a non-platform agent does not; a projector that blows up does not stop
      the message going out.
    * COLLECT reads the record the engine's `ledger.writeRunState` writes,
      lands the subject row, the platform state and the record, marks the
      strategy row used, re-derives the preferences and re-projects -- and
      collecting the same run twice changes nothing.
    * FEEDBACK appends to the log (which the database refuses to edit or
      delete), moves the subject row, and shows up in the next projection.
    * The craft projector resolves precedence and never lets a hard rule lose.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.firestore import FirestoreDB
from app.db.postgres import ConfigDatabase
from app.main import build_services, create_app
from app.services.learning import resolve_craft
from app.services.learning_store import platform_for_product
from tests.conftest import FakePublisherClient, FakeWorkspaceStore
from tests.conftest_postgres import requires_postgres

pytestmark = requires_postgres

SLUG = "acme"
LEARNING = f"clients/{SLUG}/context/learning"


@pytest.fixture
async def api(
    settings: Any,
    database: FirestoreDB,
    publisher_service: Any,
    config_database: ConfigDatabase,
    fake_workspace: FakeWorkspaceStore,
) -> AsyncIterator[AsyncClient]:
    """The real app with Postgres AND the workspace wired -- what prep runs."""

    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app,
            settings,
            database,
            publisher=publisher_service,
            config_database=config_database,
            workspace=fake_workspace,
        )
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client


async def _agent(api: AsyncClient, slug: str) -> dict[str, Any]:
    response = await api.post(
        "/agents",
        json={
            "slug": slug,
            "name": slug,
            "description": "test agent",
            "agent_type": "post_writer",
            "model": "claude-opus-5",
        },
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    prompt = await api.post(f"/agents/{created['id']}/prompts", json={"content": "Write."})
    assert prompt.status_code == 201, prompt.text
    return created


async def _dispatch(api: AsyncClient, agent: dict[str, Any]) -> str:
    """Dispatch a run and answer with the id **agent-engine** will use for it.

    THE ENGINE DOES NOT USE OUR RUN ID. It derives its own from Pub/Sub's
    message id -- `pubsub-<messageId>`, see its `queue-consumer.ts` -- and that
    is the id in every path it writes and the only one the portal ever holds.
    These tests used to write the state file under the id this service minted,
    which is a file the engine would never have produced; collect passed
    against a fixture that could not occur in production, and in production it
    answered "the run wrote no state file" about every run that had written
    one. Fixtures address the engine's id from here on.
    """

    response = await api.post(f"/agents/{agent['id']}/jobs", json={"client_slug": SLUG})
    assert response.status_code == 202, response.text
    return f"pubsub-{response.json()['pubsub_message_id']}"


def _read(workspace: FakeWorkspaceStore, path: str) -> dict[str, Any] | None:
    text = workspace.objects.get(path)
    return json.loads(text) if text else None


def _record(run_id: str, **overrides: Any) -> dict[str, Any]:
    """What `ledger.writeRunState` writes (C7 §3.1), as the x-agent test fixtures do."""

    record: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "clientSlug": SLUG,
        "productId": "x-agent",
        "platform": "x",
        "writtenAt": "2026-09-16T10:00:00.000Z",
        "deliverable": {
            "kind": "x-post",
            "goal": "attention",
            "audience": "ops leads whose intake breaks in month two",
            "whyNow": "the planned row for this stage",
            "type": "knowledge",
            "sources": ["https://a.test/1"],
        },
        "subjectRow": {
            "subject": "Why intake queues break in month two",
            "angle": "trend-observation",
            "type": "knowledge",
            "stage": "attention",
            "goal": "earn attention",
            "status": "drafted",
            "assetKind": "x-post",
            "strategyRowId": "sm-x-007",
        },
        "platformStateDelta": {"postsByUs": 1, "topics": ["Why intake queues break in month two"]},
        "voiceNotes": [{"lesson": "cut the second adjective", "fromRevision": 1}],
        "rulesApplied": ["L1-x-003"],
        "readiness": {"present": ["strategy-map"], "absent": ["craft"]},
    }
    record.update(overrides)
    return record


# --- Projection ------------------------------------------------------------------


async def test_a_client_nobody_has_taught_projects_only_the_two_empty_windows(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    response = await api.post(f"/clients/{SLUG}/learning/x/project")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["written"] == 2
    outcomes = {f["kind"]: f["outcome"] for f in body["files"]}
    assert outcomes == {
        "subject-window": "created",
        "feedback": "created",
        "platform-state": "skipped",
        "strategy-map": "skipped",
        "craft": "skipped",
        "what-works": "skipped",
        "preferences": "skipped",
    }

    window = _read(fake_workspace, f"{LEARNING}/x/subject-window.json")
    assert window is not None
    # C7 §2.0: the envelope, with provenance the reader keeps and never recomputes.
    assert window["kind"] == "subject-window"
    assert window["platform"] == "x"
    assert window["data"] == {"windowDays": 30, "rows": []}
    assert window["source"]["projectedBy"] == "portal-request"
    assert window["source"]["contentHash"].startswith("sha256:")
    assert window["source"]["rows"] == 0
    assert f"{LEARNING}/x/platform-state.json" not in fake_workspace.objects
    assert f"{LEARNING}/preferences.json" not in fake_workspace.objects


async def test_projection_is_idempotent_by_content_hash(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    await api.post(f"/clients/{SLUG}/learning/x/project")
    first = _read(fake_workspace, f"{LEARNING}/x/feedback.json")
    writes = len(fake_workspace.writes)

    second = await api.post(f"/clients/{SLUG}/learning/x/project")
    assert {
        f["outcome"] for f in second.json()["files"] if f["kind"] in ("feedback", "subject-window")
    } == {"unchanged"}
    assert len(fake_workspace.writes) == writes
    assert _read(fake_workspace, f"{LEARNING}/x/feedback.json") == first


async def test_preferences_strategy_map_and_craft_reach_the_files_in_c7_shapes(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    prefs = await api.put(
        f"/clients/{SLUG}/learning/preferences",
        json={"neverTopics": ["four-day weeks"], "standingInstructions": ["no competitor names"]},
    )
    assert prefs.status_code == 200, prefs.text

    strategy = await api.put(
        f"/clients/{SLUG}/learning/x/strategy-map",
        json={
            "source": "manual",
            "audience": [{"role": "Head of Ops", "problems": ["intake"]}],
            "rows": [
                {
                    "id": "sm-x-007",
                    "stage": "attention",
                    "idea": "Why intake queues break in month two",
                    "problem": "intake breaks",
                },
                {
                    "id": "sm-x-008",
                    "stage": "expertise",
                    "idea": "The one metric ops leads never track",
                },
            ],
        },
    )
    assert strategy.status_code == 200, strategy.text

    craft = await api.put(
        "/learning/craft-rules",
        json={
            "rules": [
                {
                    "id": "L1-x-003",
                    "platform": "x",
                    "layer": "L1",
                    "kind": "hard",
                    "rule": "No link in the post body",
                    "metric": "reach",
                },
                {"id": "L1-x-011", "platform": "x", "layer": "L1", "rule": "Open with a question"},
                {
                    "id": "L2-saas-x-002",
                    "platform": "x",
                    "layer": "L2",
                    "sector": "saas",
                    "rule": "Lead with a benchmark",
                    "metric": "replies",
                },
                {
                    "id": "L3-acme-x-001",
                    "platform": "x",
                    "layer": "L3",
                    "clientSlug": SLUG,
                    "rule": "Open with a declarative",
                    "sampleSize": 12,
                    "overrides": ["L1-x-011", "L1-x-003"],
                },
                {
                    "id": "L2-fintech-x-001",
                    "platform": "x",
                    "layer": "L2",
                    "sector": "fintech",
                    "rule": "not for acme",
                },
            ]
        },
    )
    assert craft.status_code == 200, craft.text
    settings = await api.put(
        f"/clients/{SLUG}/learning/x/settings", json={"sector": "saas", "antiRepeatDays": 45}
    )
    assert settings.status_code == 200, settings.text

    projected = await api.post(f"/clients/{SLUG}/learning/x/project")
    outcomes = {f["kind"]: f["outcome"] for f in projected.json()["files"]}
    assert outcomes["preferences"] in ("created", "unchanged")  # PUT preferences already projected
    assert outcomes["strategy-map"] in ("created", "unchanged")
    assert outcomes["craft"] == "created"

    prefs_file = _read(fake_workspace, f"{LEARNING}/preferences.json")
    assert prefs_file is not None
    assert "platform" not in prefs_file  # client-wide (C7 §2.0)
    assert prefs_file["data"]["neverTopics"] == ["four-day weeks"]
    assert prefs_file["data"]["standingInstructions"] == ["no competitor names"]
    assert prefs_file["data"]["voiceNotes"] == []

    strategy_file = _read(fake_workspace, f"{LEARNING}/x/strategy-map.json")
    assert strategy_file is not None
    assert strategy_file["data"]["defaultMix"] == {"attention": 3, "expertise": 2, "decide": 1}
    assert [r["id"] for r in strategy_file["data"]["rows"]] == ["sm-x-007", "sm-x-008"]
    assert strategy_file["data"]["rows"][0] == {
        "id": "sm-x-007",
        "problem": "intake breaks",
        "stage": "attention",
        "idea": "Why intake queues break in month two",
        "status": "open",
    }

    craft_file = _read(fake_workspace, f"{LEARNING}/x/craft.json")
    assert craft_file is not None
    ids = [r["id"] for r in craft_file["data"]["rules"]]
    # L1 for the platform, L2 for acme's sector only, L3 for acme.
    assert ids == ["L1-x-003", "L1-x-011", "L2-saas-x-002", "L3-acme-x-001"]
    assert {
        "id": "L1-x-003",
        "layer": "L1",
        "kind": "hard",
        "rule": "No link in the post body",
        "metric": "reach",
    } in craft_file["data"]["rules"]
    # The L3 rule beats the L1 default it names; its claim over the hard rule is dropped.
    assert craft_file["data"]["overrides"] == [{"winner": "L3-acme-x-001", "loser": "L1-x-011"}]

    window = _read(fake_workspace, f"{LEARNING}/x/subject-window.json")
    assert window is not None
    assert window["data"]["windowDays"] == 45

    # The same view, served from the tables for a page with no bucket access.
    view = await api.get(f"/clients/{SLUG}/learning/x")
    assert view.status_code == 200, view.text
    assert view.json()["craft"]["overrides"] == craft_file["data"]["overrides"]
    assert view.json()["preferences"]["neverTopics"] == ["four-day weeks"]
    assert view.json()["what-works"] is None


def test_the_craft_resolver_never_lets_a_hard_rule_lose() -> None:
    resolved = resolve_craft(
        [
            {"id": "L1-x-001", "layer": "L1", "kind": "hard", "rule": "a", "overrides": []},
            {"id": "L1-x-002", "layer": "L1", "kind": "default", "rule": "b", "overrides": []},
            {
                "id": "L3-c-x-001",
                "layer": "L3",
                "kind": "default",
                "rule": "c",
                "overrides": ["L1-x-001", "L1-x-002", "gone"],
            },
        ]
    )
    assert resolved["overrides"] == [{"winner": "L3-c-x-001", "loser": "L1-x-002"}]
    assert all("overrides" not in r for r in resolved["rules"])


# --- Dispatch --------------------------------------------------------------------


async def test_dispatching_a_platform_agent_projects_before_publishing(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore, fake_publisher_client: FakePublisherClient
) -> None:
    agent = await _agent(api, "x-agent")
    assert f"{LEARNING}/x/subject-window.json" not in fake_workspace.objects

    response = await api.post(
        f"/agents/{agent['id']}/jobs",
        json={"client_slug": SLUG, "input": {"slotStage": "expertise"}},
    )
    assert response.status_code == 202, response.text

    window = _read(fake_workspace, f"{LEARNING}/x/subject-window.json")
    assert window is not None
    assert window["source"]["projectedBy"] == "middleware-dispatch"
    # The slot's stage rides on the run input the engine reads (C3 / C7 §2).
    _topic, data, _attrs = fake_publisher_client.published[0]
    assert json.loads(data.decode("utf-8"))["input"] == {"slotStage": "expertise"}


async def test_dispatching_a_non_platform_agent_projects_nothing(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    agent = await _agent(api, "seo-geo-agent")
    response = await api.post(f"/agents/{agent['id']}/jobs", json={"client_slug": SLUG})
    assert response.status_code == 202, response.text
    assert not any(path.startswith(f"{LEARNING}/") for path in fake_workspace.objects)


async def test_a_projector_that_blows_up_does_not_stop_the_dispatch(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore, fake_publisher_client: FakePublisherClient
) -> None:
    agent = await _agent(api, "linkedin-agent")

    def explode(path: str) -> str | None:
        raise RuntimeError("bucket is on fire")

    fake_workspace.read_text = explode  # type: ignore[method-assign]
    response = await api.post(f"/agents/{agent['id']}/jobs", json={"client_slug": SLUG})
    assert response.status_code == 202, response.text
    assert len(fake_publisher_client.published) == 1


def test_only_platform_agents_have_a_platform() -> None:
    assert platform_for_product("x-agent") == "x"
    assert platform_for_product("linkedin-agent") == "linkedin"
    assert platform_for_product("tiktok-agent") == "tiktok"
    assert platform_for_product("seo-geo-agent") is None
    assert platform_for_product(None) is None


def test_all_three_tiktok_products_share_one_platform() -> None:
    """D08 (SCRUM-455): three agents, one TikTok account, one subject history.

    The stores are keyed on the platform rather than the product precisely so
    this holds -- a clip and a scripted short must not be able to repeat each
    other's subject just because they were sold as different cards.
    """

    assert platform_for_product("tiktok-clipping-agent") == "tiktok"
    assert platform_for_product("tiktok-editing-agent") == "tiktok"
    assert platform_for_product("tiktok-content-design-agent") == "tiktok"


def test_branded_shorts_is_the_editing_agents_old_name_and_now_projects() -> None:
    """It used to map to ``None``, which is why it never learned anything.

    The bug was invisible: every branded-shorts run drafted, delivered and
    collected nothing, and looked completely healthy from the outside. That is
    the failure mode ``PRODUCT_PLATFORM_OVERRIDES`` exists to make impossible.
    """

    assert platform_for_product("branded-shorts-agent") == "tiktok"


# --- Collect --------------------------------------------------------------------


async def test_collect_lands_the_record_the_subject_row_and_the_platform_state(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore, config_database: ConfigDatabase
) -> None:
    agent = await _agent(api, "x-agent")
    await api.put(
        f"/clients/{SLUG}/learning/x/strategy-map",
        json={
            "rows": [
                {
                    "id": "sm-x-007",
                    "stage": "attention",
                    "idea": "Why intake queues break in month two",
                }
            ]
        },
    )
    run_id = await _dispatch(api, agent)

    # What the engine leaves behind (ledger.writeRunState).
    fake_workspace.objects[f"clients/{SLUG}/state/runs/{run_id}.json"] = json.dumps(_record(run_id))
    fake_workspace.objects[f"clients/{SLUG}/state/x/platform-state.json"] = json.dumps(
        {
            "postsByUs": 1,
            "topics": ["Why intake queues break in month two"],
            "account": {"handle": "@acmehq"},
            "contributingRuns": [run_id],
        }
    )

    response = await api.post(f"/runs/{run_id}/collect")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "runId": run_id,
        "collected": True,
        "reason": "",
        "clientSlug": SLUG,
        "platform": "x",
        "subjectRowId": body["subjectRowId"],
        "recordChanged": True,
        "platformStateCollected": True,
        "strategyRowsCollected": 0,
        "reprojected": body["reprojected"],
    }
    assert body["subjectRowId"]
    assert body["reprojected"] >= 2  # the window and the platform state now have content

    # B1: the subject row, with the D11 goal line beside it.
    rows = (await api.get(f"/clients/{SLUG}/learning/x/subjects")).json()["rows"]
    assert len(rows) == 1
    assert rows[0] == {
        "id": body["subjectRowId"],
        "runId": run_id,
        "subject": "Why intake queues break in month two",
        "angle": "trend-observation",
        "type": "knowledge",
        "stage": "attention",
        "goal": "earn attention",
        "status": "drafted",
        "assetKind": "x-post",
        "strategyRowId": "sm-x-007",
        "audience": "ops leads whose intake breaks in month two",
        "whyNow": "the planned row for this stage",
        "draftedAt": "2026-09-16T10:00:00Z",
    }

    # The strategy row the run took is used; the next projection says so.
    strategy_file = _read(fake_workspace, f"{LEARNING}/x/strategy-map.json")
    assert strategy_file is not None
    assert strategy_file["data"]["rows"][0]["status"] == "used"
    assert strategy_file["data"]["rows"][0]["usedByRunId"] == run_id

    # The platform state came back whole and is what the next run reads (§2.1).
    state_file = _read(fake_workspace, f"{LEARNING}/x/platform-state.json")
    assert state_file is not None
    assert state_file["data"]["postsByUs"] == 1
    assert state_file["data"]["account"] == {"handle": "@acmehq"}
    assert state_file["source"]["projectedBy"] == "middleware-collect"

    # The subject is in the window the next run reads (§2.2), so it is not proposed again.
    window = _read(fake_workspace, f"{LEARNING}/x/subject-window.json")
    assert window is not None
    assert [r["subject"] for r in window["data"]["rows"]] == [
        "Why intake queues break in month two"
    ]

    # A review-cycle voice note became a preference (Craft 11 §3), from this run.
    prefs_response = await api.get(f"/clients/{SLUG}/learning/preferences")
    prefs = prefs_response.json()
    assert isinstance(prefs, dict) and "voiceNotes" in prefs, (
        prefs_response.status_code,
        prefs_response.text,
    )
    assert prefs["voiceNotes"] == [{"lesson": "cut the second adjective", "fromRunId": run_id}], (
        prefs
    )

    # The record is kept verbatim.
    stored = await config_database.fetchrow(
        "select * from run_state_records where run_id = $1", run_id
    )
    assert stored is not None
    assert stored["record"]["readiness"] == {"present": ["strategy-map"], "absent": ["craft"]}

    # Collecting again is a no-op that says so.
    again = (await api.post(f"/runs/{run_id}/collect")).json()
    assert again["collected"] is True
    assert again["recordChanged"] is False
    assert again["reprojected"] == 0
    assert len((await api.get(f"/clients/{SLUG}/learning/x/subjects")).json()["rows"]) == 1


async def test_collect_on_a_run_that_wrote_nothing_is_an_answer_not_an_error(
    api: AsyncClient,
) -> None:
    agent = await _agent(api, "x-agent")
    run_id = await _dispatch(api, agent)

    response = await api.post(f"/runs/{run_id}/collect")
    assert response.status_code == 200, response.text
    assert response.json()["collected"] is False
    assert "wrote no state/runs" in response.json()["reason"]

    unknown = await api.post("/runs/never-dispatched/collect")
    assert unknown.status_code == 200
    assert unknown.json()["collected"] is False
    assert "not registered" in unknown.json()["reason"]


async def test_collect_takes_a_strategy_map_a_setup_run_built(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    agent = await _agent(api, "x-agent")
    run_id = await _dispatch(api, agent)
    fake_workspace.objects[f"clients/{SLUG}/state/runs/{run_id}.json"] = json.dumps(
        _record(run_id, subjectRow={"subject": "first post", "stage": "attention"})
    )
    fake_workspace.objects[f"clients/{SLUG}/state/x/strategy-map.json"] = json.dumps(
        {
            "platform": "x",
            "source": "setup-run",
            "builtAt": "2026-09-16T09:00:00Z",
            "audience": [{"role": "Head of Ops"}],
            "rows": [
                {"id": "sm-x-001", "stage": "attention", "idea": "A", "problem": "p"},
                {"id": "sm-x-002", "stage": "decide", "idea": "B"},
                {"id": "broken", "stage": "awareness", "idea": "not a stage"},
            ],
        }
    )
    body = (await api.post(f"/runs/{run_id}/collect")).json()
    assert body["collected"] is True
    assert body["strategyRowsCollected"] == 2
    strategy_file = _read(fake_workspace, f"{LEARNING}/x/strategy-map.json")
    assert strategy_file is not None
    assert strategy_file["data"]["source"] == "setup-run"
    assert [r["id"] for r in strategy_file["data"]["rows"]] == ["sm-x-001", "sm-x-002"]


async def test_collect_resolves_the_run_from_either_id_and_always_stores_the_engines(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    """The portal only ever holds ``pubsub-<messageId>``; we mint something else.

    This is the bug that kept the loop open end to end. Reconcile can pass
    nothing but the engine's id, collect read ``state/runs/<our uuid>.json``,
    and the honest-looking answer "the run wrote no state file" came back for
    every run in production -- including the ones that had written one.

    Both spellings must land on the same record, and what is stored must be the
    engine's id, because that is the id the state files, the deliverables and
    the portal all agree on.
    """

    agent = await _agent(api, "x-agent")
    dispatched = await api.post(f"/agents/{agent['id']}/jobs", json={"client_slug": SLUG})
    middleware_run_id = dispatched.json()["run"]["id"]
    engine_run_id = f"pubsub-{dispatched.json()['pubsub_message_id']}"
    assert middleware_run_id != engine_run_id

    fake_workspace.objects[f"clients/{SLUG}/state/runs/{engine_run_id}.json"] = json.dumps(
        _record(engine_run_id)
    )

    # What reconcile actually sends.
    from_engine = (await api.post(f"/runs/{engine_run_id}/collect")).json()
    assert from_engine["collected"] is True, from_engine
    assert from_engine["runId"] == engine_run_id
    assert from_engine["recordChanged"] is True

    # And our own id resolves to the same run -- idempotently, not as a second one.
    from_ours = (await api.post(f"/runs/{middleware_run_id}/collect")).json()
    assert from_ours["collected"] is True, from_ours
    assert from_ours["runId"] == engine_run_id
    assert from_ours["recordChanged"] is False

    rows = (await api.get(f"/clients/{SLUG}/learning/x/subjects")).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["runId"] == engine_run_id


# --- Feedback (B2) --------------------------------------------------------------


async def test_feedback_moves_the_subject_row_and_reaches_the_next_projection(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore, config_database: ConfigDatabase
) -> None:
    agent = await _agent(api, "x-agent")
    run_id = await _dispatch(api, agent)
    fake_workspace.objects[f"clients/{SLUG}/state/runs/{run_id}.json"] = json.dumps(_record(run_id))
    await api.post(f"/runs/{run_id}/collect")

    response = await api.post(
        f"/clients/{SLUG}/learning/feedback",
        json={
            "platform": "x",
            "action": "posted_with_edits",
            "runId": run_id,
            "account": "@acmehq",
            "originalText": "We are thrilled to announce",
            "finalText": "We shipped",
            "actor": "lola@acme.test",
            "at": "2026-09-16T12:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["duplicate"] is False
    assert body["subjectRowsMoved"] == 1
    assert body["row"]["action"] == "posted_with_edits"

    rows = (await api.get(f"/clients/{SLUG}/learning/x/subjects")).json()["rows"]
    assert rows[0]["status"] == "posted"
    assert rows[0]["postedAt"] == "2026-09-16T12:00:00Z"

    # The next run sees the edit in the shape feedbackForPrompt reads (§2.3).
    feedback_file = _read(fake_workspace, f"{LEARNING}/x/feedback.json")
    assert feedback_file is not None
    assert feedback_file["source"]["rows"] == 1
    row = feedback_file["data"]["rows"][0]
    assert row["action"] == "posted_with_edits"
    assert row["originalText"] == "We are thrilled to announce"
    assert row["finalText"] == "We shipped"
    assert row["at"] == "2026-09-16T12:00:00Z"
    assert row["runId"] == run_id

    # A skip with a reason, and a note that becomes a voice lesson.
    skipped = await api.post(
        f"/clients/{SLUG}/learning/feedback",
        json={"platform": "x", "action": "skipped", "reason": "too salesy"},
    )
    assert skipped.status_code == 201
    noted = await api.post(
        f"/clients/{SLUG}/learning/feedback",
        json={"platform": "x", "action": "note", "reason": "never open with a question"},
    )
    assert noted.status_code == 201
    prefs = (await api.get(f"/clients/{SLUG}/learning/preferences")).json()
    assert {n["lesson"] for n in prefs["voiceNotes"]} == {
        "cut the second adjective",
        "never open with a question",
    }
    assert prefs["derivedFromCount"] == 3

    # The database, not the code, refuses to rewrite history.
    with pytest.raises(asyncpg.PostgresError):
        await config_database.execute(
            "update client_feedback_log set reason = 'x' where run_id = $1", run_id
        )
    with pytest.raises(asyncpg.PostgresError):
        await config_database.execute("delete from client_feedback_log where run_id = $1", run_id)


async def test_an_import_with_a_source_id_lands_once(api: AsyncClient) -> None:
    body = {"platform": "x", "action": "skipped", "reason": "dup", "sourceId": "portal-evt-1"}
    first = await api.post(f"/clients/{SLUG}/learning/feedback", json=body)
    second = await api.post(f"/clients/{SLUG}/learning/feedback", json=body)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert (await api.get(f"/clients/{SLUG}/learning/x")).json()["feedback"]["rows"].__len__() == 1


async def test_an_edit_without_both_texts_and_an_unknown_action_are_refused(
    api: AsyncClient,
) -> None:
    half = await api.post(
        f"/clients/{SLUG}/learning/feedback",
        json={"platform": "x", "action": "posted_with_edits", "finalText": "only the after"},
    )
    assert half.status_code == 422, half.text
    bad = await api.post(
        f"/clients/{SLUG}/learning/feedback", json={"platform": "x", "action": "loved"}
    )
    assert bad.status_code == 422


# --- Without a bucket ---------------------------------------------------------------


async def test_without_a_bucket_the_tables_work_and_projection_says_so(
    settings: Any, database: FirestoreDB, publisher_service: Any, config_database: ConfigDatabase
) -> None:
    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app, settings, database, publisher=publisher_service, config_database=config_database
        )
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        async with app.router.lifespan_context(app):
            prefs = await api.put(
                f"/clients/{SLUG}/learning/preferences", json={"neverTopics": ["pricing"]}
            )
            assert prefs.status_code == 200, prefs.text
            assert (await api.get(f"/clients/{SLUG}/learning/x")).json()["preferences"][
                "neverTopics"
            ] == ["pricing"]
            projected = await api.post(f"/clients/{SLUG}/learning/x/project")
            assert projected.status_code == 503
            assert "GCS_ARTIFACTS_BUCKET" in projected.json()["detail"]


async def test_repeated_edits_become_a_voice_lesson_and_a_post_becomes_a_like(
    api: AsyncClient, fake_workspace: FakeWorkspaceStore
) -> None:
    """B2 (SCRUM-494): the counted half of the preferences.

    ``client_preferences``' own comment has always said voice notes are derived
    "from edits", and the derivation never read one — it carried what a run or a
    person had already said in words. ``likes`` had no writer at all.

    This is the whole claim end to end: three edits that take the same word out,
    two skips with the same reason, and one draft posted untouched, through the
    real endpoint and the real transaction, landing in the file the next run
    reads.
    """

    agent = await _agent(api, "x-agent")
    run_id = await _dispatch(api, agent)
    fake_workspace.objects[f"clients/{SLUG}/state/runs/{run_id}.json"] = json.dumps(_record(run_id))
    await api.post(f"/runs/{run_id}/collect")

    # Three edits, each taking "leverage" out and none putting it back.
    for original, final in (
        ("We leverage our platform to unlock growth", "We help you grow"),
        ("Leverage the data you already have to drive outcomes", "Use the data you have"),
        ("A quick way to leverage the quarter and build momentum", "One way to use the quarter"),
    ):
        edited = await api.post(
            f"/clients/{SLUG}/learning/feedback",
            json={
                "platform": "x",
                "action": "posted_with_edits",
                "originalText": original,
                "finalText": final,
            },
        )
        assert edited.status_code == 201, edited.text

    # The same reason twice: tone, not a topic — so it must NOT reach never_topics.
    for _ in range(2):
        await api.post(
            f"/clients/{SLUG}/learning/feedback",
            json={"platform": "x", "action": "skipped", "reason": "too salesy"},
        )

    # And one the client published without touching, which is the only
    # endorsement the log actually contains.
    posted = await api.post(
        f"/clients/{SLUG}/learning/feedback",
        json={"platform": "x", "action": "posted", "runId": run_id},
    )
    assert posted.status_code == 201, posted.text

    prefs = (await api.get(f"/clients/{SLUG}/learning/preferences")).json()
    lessons = [note["lesson"] for note in prefs["voiceNotes"]]

    assert 'Takes "leverage" out: removed in 3 edits and never published once.' in lessons
    assert any(lesson.startswith("Rewrites shorter") for lesson in lessons)
    assert 'Skipped 2 drafts for the same reason: "too salesy".' in lessons
    # The lesson somebody STATED is kept beside the counted ones, not replaced.
    assert "cut the second adjective" in lessons
    # A skip reason is tone. Banning the topic is a person's decision (the
    # table's own design), and this derivation must never make it for them.
    assert prefs["neverTopics"] == []

    # The like names the POST rather than a run id nobody can read, and says why
    # it is there — nobody clicked a heart. The subject is read back off the
    # subject row rather than restated here, because the join is the thing under
    # test: a like that lost it would still look right against a literal.
    subject = (await api.get(f"/clients/{SLUG}/learning/x/subjects")).json()["rows"][0]["subject"]
    assert prefs["likes"] == [
        {
            "why": "posted as written",
            "subject": subject,
            "runId": run_id,
            "at": prefs["likes"][0]["at"],
        }
    ]

    # THE ORDER IS THE POINT, and it is the part a reader will get wrong. The
    # engine takes the LAST eight (`learning-context.ts`'s `slice(-8)`), so the
    # best-evidenced lesson has to be at the END of the list.
    evidence = [note.get("evidence", 1) for note in prefs["voiceNotes"]]
    assert evidence == sorted(evidence)

    # And it reaches the file the next run actually reads.
    await api.post(f"/clients/{SLUG}/learning/x/project")
    preferences_file = _read(fake_workspace, f"{LEARNING}/preferences.json")
    assert preferences_file is not None
    projected = [note["lesson"] for note in preferences_file["data"]["voiceNotes"]]
    assert 'Takes "leverage" out: removed in 3 edits and never published once.' in projected
