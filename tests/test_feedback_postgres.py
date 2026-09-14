"""Run feedback on Postgres (S11 / SCRUM-224).

What these pin, against a real PostgreSQL 16 with the real migrations applied:

    * The API's contract over ``config.run_feedback`` is the one
      ``tests/test_feedback.py`` pins over the Firestore collection. Same
      routes, same bodies, same shapes. A client cannot tell which store it hit.
    * The join the move exists for: a verdict points at the prompt version
      that was in the box when the run happened, and the raw engine reference
      is kept beside it whether or not that resolves.
    * The database refuses what the Firestore version could only promise: a
      verdict is never edited or deleted, promotion happens once, and the
      rating and status vocabularies hold whatever the code above does.

The ``api`` fixture here wires the config database in, so ``build_services``
picks the Postgres store on its own -- the same way a deployed instance with
``CONFIG_DB_DSN`` set does. Nothing in these tests reaches around the service
to choose a store.
"""

from __future__ import annotations

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
from app.services.engine_prompts import ENGINE_PROMPT_VERSIONS, ENGINE_PROMPTS
from tests.conftest_postgres import requires_postgres

pytestmark = requires_postgres

PROMPT_ID = "x-draft"
ENGINE_VERSION = "2"


@pytest.fixture
async def api(
    settings: Any,
    database: FirestoreDB,
    publisher_service: Any,
    config_database: ConfigDatabase,
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app, settings, database, publisher=publisher_service,
            config_database=config_database,
        )
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client


@pytest.fixture
async def agent(api: AsyncClient) -> dict[str, Any]:
    response = await api.post(
        "/agents",
        json={
            "slug": "post-writer",
            "name": "Post Writer",
            "description": "Writes social posts",
            "agent_type": "post_writer",
            "model": "claude-opus-5",
            "tags": ["content"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    prompt = await api.post(
        f"/agents/{created['id']}/prompts",
        json={"content": "You are a concise social post writer.", "notes": "first"},
    )
    assert prompt.status_code == 201, prompt.text
    return created


async def register_run(
    api: AsyncClient, agent: dict[str, Any], run_id: str = "run-1", **fields: Any
) -> dict[str, Any]:
    """A run that has reported a successful result, with an output to judge.

    Registered the way a portal that publishes its own payload does (``POST
    /runs``), because that is the path on which the caller names the ENGINE
    prompt the run was produced with -- ``prompt_id`` / ``prompt_version`` as
    the pinned skillRef has them. Dispatching through ``/jobs`` records the
    middleware's own system prompt there instead, which is a different prompt.
    """

    registered = await api.post(
        f"/agents/{agent['id']}/runs",
        json={
            "client_slug": "acme",
            "run_id": run_id,
            "job_type": "social_post",
            "input_payload": {"input": {"topic": "cold brew"}},
            **fields,
        },
    )
    assert registered.status_code == 201, registered.text
    reported = await api.patch(
        f"/agents/{agent['id']}/runs/{run_id}",
        json={"status": "succeeded", "output": {"content": "Cold brew is great."}},
    )
    assert reported.status_code == 200, reported.text
    return reported.json()


@pytest.fixture
async def run(api: AsyncClient, agent: dict[str, Any]) -> dict[str, Any]:
    return await register_run(api, agent)


async def verdict(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any], **body: Any
) -> dict[str, Any]:
    response = await api.post(
        f"/agents/{agent['id']}/runs/{run['id']}/feedback",
        json={"rating": 5, "status": "approved", **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- The store the service chose --------------------------------------------


async def test_the_service_chose_postgres_because_the_database_is_wired(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any], config_database: ConfigDatabase
) -> None:
    """The one assertion that says which store is under the API.

    Everything below is contract; this is the fact the contract is being
    checked against. Two verdicts through the API, two rows in the table.
    """

    await verdict(api, agent, run, reviewer="shlomi")
    await verdict(api, agent, run, rating=2, status="needs_changes", reviewer="lola")

    rows = await config_database.fetch("select run_id, rating, reviewer from run_feedback")
    assert sorted((r["run_id"], r["rating"], r["reviewer"]) for r in rows) == [
        ("run-1", 2, "lola"),
        ("run-1", 5, "shlomi"),
    ]


# --- Same contract as the Firestore suite -----------------------------------


async def test_submit_and_read_back_over_the_same_routes(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    body = await verdict(
        api, agent, run,
        correction_notes="shorten the opening", reviewer="shlomi", tags=["tone"],
    )
    assert body["run_id"] == run["id"]
    assert body["agent_id"] == agent["id"]
    assert body["rating"] == 5
    assert body["status"] == "approved"
    assert body["tags"] == ["tone"]
    assert body["promoted_example_id"] is None
    assert body["created_at"] and body["updated_at"]

    on_run = (await api.get(f"/agents/{agent['id']}/runs/{run['id']}/feedback")).json()
    assert [item["id"] for item in on_run] == [body["id"]]

    detail = (await api.get(f"/agents/{agent['id']}/runs/{run['id']}")).json()
    assert [item["id"] for item in detail["feedback"]] == [body["id"]]


async def test_a_run_lists_its_verdicts_oldest_first(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    first = await verdict(api, agent, run, rating=2, reviewer="a")
    second = await verdict(api, agent, run, rating=5, reviewer="b")
    third = await verdict(api, agent, run, rating=3, reviewer="c")

    listed = (await api.get(f"/agents/{agent['id']}/runs/{run['id']}/feedback")).json()
    assert [item["id"] for item in listed] == [first["id"], second["id"], third["id"]]


async def test_agent_listing_is_best_rated_first_with_filters_and_paging(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    for rating, status in ((2, "rejected"), (5, "approved"), (4, "approved"), (3, "needs_changes")):
        await verdict(api, agent, run, rating=rating, status=status)

    page = (await api.get(f"/agents/{agent['id']}/feedback")).json()
    assert [item["rating"] for item in page["items"]] == [5, 4, 3, 2]
    assert page["has_more"] is False

    approved = (
        await api.get(f"/agents/{agent['id']}/feedback", params={"status": "approved"})
    ).json()
    assert [item["rating"] for item in approved["items"]] == [5, 4]

    at_least_three = (
        await api.get(f"/agents/{agent['id']}/feedback", params={"min_rating": 3})
    ).json()
    assert [item["rating"] for item in at_least_three["items"]] == [5, 4, 3]

    first_page = (await api.get(f"/agents/{agent['id']}/feedback", params={"limit": 2})).json()
    assert [item["rating"] for item in first_page["items"]] == [5, 4]
    assert first_page["has_more"] is True
    second_page = (
        await api.get(f"/agents/{agent['id']}/feedback", params={"limit": 2, "offset": 2})
    ).json()
    assert [item["rating"] for item in second_page["items"]] == [3, 2]
    assert second_page["has_more"] is False


async def test_feedback_of_another_agent_is_not_reachable(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    body = await verdict(api, agent, run)

    other = await api.post(
        "/agents",
        json={
            "slug": "other", "name": "Other", "agent_type": "post_writer", "model": "claude-opus-5",
        },
    )
    assert other.status_code == 201, other.text

    response = await api.post(
        f"/agents/{other.json()['id']}/feedback/{body['id']}/promote", json={}
    )
    assert response.status_code == 404


async def test_an_unknown_feedback_id_of_any_shape_is_404_not_500(
    api: AsyncClient, agent: dict[str, Any]
) -> None:
    # The API accepts any string; the column is a uuid. Both a well-formed
    # uuid that names no row and a string that could never be one are "not
    # found", never a driver error surfaced as 500.
    for feedback_id in ("00000000-0000-0000-0000-000000000000", "not-a-uuid", "fb_123"):
        response = await api.post(f"/agents/{agent['id']}/feedback/{feedback_id}/promote", json={})
        assert response.status_code == 404, (feedback_id, response.text)


async def test_examples_and_promotion_work_over_postgres(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any]
) -> None:
    body = await verdict(api, agent, run, corrected_output="Cold brew, but shorter.")

    examples = (await api.get(f"/agents/{agent['id']}/feedback/examples")).json()
    assert [e["feedback_id"] for e in examples["items"]] == [body["id"]]
    assert examples["items"][0]["assistant_output"] == "Cold brew, but shorter."
    assert examples["items"][0]["already_promoted"] is False

    promoted = await api.post(f"/agents/{agent['id']}/feedback/{body['id']}/promote", json={})
    assert promoted.status_code == 201, promoted.text
    example = promoted.json()
    assert example["assistant_output"] == "Cold brew, but shorter."

    again = await api.post(f"/agents/{agent['id']}/feedback/{body['id']}/promote", json={})
    assert again.status_code == 409, again.text

    after = (await api.get(f"/agents/{agent['id']}/runs/{run['id']}/feedback")).json()
    assert after[0]["promoted_example_id"] == example["id"]
    examples = (await api.get(f"/agents/{agent['id']}/feedback/examples")).json()
    assert examples["items"][0]["already_promoted"] is True


# --- The join this table exists for -----------------------------------------


async def publish_engine_prompt(firestore: FirestoreDB, content: str) -> None:
    await firestore.document(ENGINE_PROMPTS, PROMPT_ID).set({"latestVersion": ENGINE_VERSION})
    await firestore.document(ENGINE_PROMPT_VERSIONS, f"{PROMPT_ID}@{ENGINE_VERSION}").set(
        {"content": content, "updated_by": "someone@karoslabs.com"}
    )


async def test_a_verdict_points_at_the_prompt_version_that_produced_the_run(
    api: AsyncClient,
    agent: dict[str, Any],
    database: FirestoreDB,
    config_database: ConfigDatabase,
) -> None:
    """The reason to move: "which prompt produced the output this person rated 2".

    Two content versions of x-draft@2 are saved through S7's store. A run
    produced against the first is judged; the verdict's FK names version 1,
    not the newer version 2 that happens to be live now.
    """

    await publish_engine_prompt(database, "Draft a post.")
    saved = await api.put(
        f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}",
        json={"content": "Draft a warm post."},
    )
    assert saved.status_code == 200, saved.text
    # Saving through the store imported the pre-existing content as v1 and
    # wrote the new content as v2.
    versions = await config_database.fetch(
        """
        select pv.id, pv.version from prompt_versions pv
          join prompts p on p.id = pv.prompt_id
         where p.engine_prompt_id = $1 and p.engine_version = $2
         order by pv.version
        """,
        PROMPT_ID,
        ENGINE_VERSION,
    )
    assert [v["version"] for v in versions] == [1, 2]

    # A run produced by the engine against x-draft@2 -- registered after v2
    # was saved, so the newest version at or before the run is v2.
    run = await register_run(api, agent, "run-v2", prompt_id=PROMPT_ID, prompt_version=2)
    body = await verdict(api, agent, run, rating=2, status="needs_changes")

    row = await config_database.fetchrow(
        "select prompt_version_id, engine_prompt_id, engine_prompt_version "
        "from run_feedback where id = $1",
        body["id"],
    )
    assert row is not None
    assert str(row["prompt_version_id"]) == str(versions[1]["id"])
    assert (row["engine_prompt_id"], row["engine_prompt_version"]) == (PROMPT_ID, ENGINE_VERSION)

    # And the join answers the question in one query.
    criticised = await config_database.fetchrow(
        """
        select pv.version, pv.content, f.rating
          from run_feedback f
          join prompt_versions pv on pv.id = f.prompt_version_id
         where f.run_id = $1
        """,
        "run-v2",
    )
    assert criticised is not None
    assert (criticised["version"], criticised["content"], criticised["rating"]) == (
        2, "Draft a warm post.", 2,
    )


async def test_a_run_whose_prompt_has_no_versions_here_still_records_the_raw_reference(
    api: AsyncClient, agent: dict[str, Any], config_database: ConfigDatabase
) -> None:
    run = await register_run(api, agent, "run-unsaved", prompt_id="never-saved", prompt_version=7)
    body = await verdict(api, agent, run)

    row = await config_database.fetchrow(
        "select prompt_version_id, engine_prompt_id, engine_prompt_version "
        "from run_feedback where id = $1",
        body["id"],
    )
    assert row is not None
    assert row["prompt_version_id"] is None
    assert (row["engine_prompt_id"], row["engine_prompt_version"]) == ("never-saved", "7")


async def test_a_run_that_named_no_prompt_records_neither(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any], config_database: ConfigDatabase
) -> None:
    body = await verdict(api, agent, run)
    row = await config_database.fetchrow(
        "select prompt_version_id, engine_prompt_id, engine_prompt_version, client_slug "
        "from run_feedback where id = $1",
        body["id"],
    )
    assert row is not None
    assert (row["prompt_version_id"], row["engine_prompt_id"], row["engine_prompt_version"]) == (
        None, None, None,
    )
    assert row["client_slug"] == "acme"


# --- What the database refuses ----------------------------------------------


async def test_a_verdict_cannot_be_edited_or_deleted(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any], config_database: ConfigDatabase
) -> None:
    """History is preserved by the table, not by nobody writing the UPDATE."""

    body = await verdict(api, agent, run, rating=2, reviewer="shlomi")

    with pytest.raises(asyncpg.PostgresError, match="history"):
        await config_database.execute(
            "update run_feedback set rating = 5 where id = $1", body["id"]
        )
    with pytest.raises(asyncpg.PostgresError, match="history"):
        await config_database.execute(
            "update run_feedback set reviewer = 'someone else' where id = $1", body["id"]
        )
    with pytest.raises(asyncpg.PostgresError, match="cannot be deleted"):
        await config_database.execute("delete from run_feedback where id = $1", body["id"])

    still = await config_database.fetchrow(
        "select rating, reviewer from run_feedback where id = $1", body["id"]
    )
    assert still is not None and (still["rating"], still["reviewer"]) == (2, "shlomi")


async def test_promotion_is_recorded_once_and_only_promotion(
    api: AsyncClient, agent: dict[str, Any], run: dict[str, Any], config_database: ConfigDatabase
) -> None:
    body = await verdict(api, agent, run)

    # The one column that may move, moving.
    await config_database.execute(
        "update run_feedback set promoted_example_id = 'ex-1' where id = $1", body["id"]
    )
    # It does not move twice, and it does not un-move.
    with pytest.raises(asyncpg.PostgresError, match="already promoted"):
        await config_database.execute(
            "update run_feedback set promoted_example_id = 'ex-2' where id = $1", body["id"]
        )
    with pytest.raises(asyncpg.PostgresError, match="already promoted"):
        await config_database.execute(
            "update run_feedback set promoted_example_id = null where id = $1", body["id"]
        )


async def test_the_vocabularies_hold_below_the_api(config_database: ConfigDatabase) -> None:
    """A rating of 6 or a status outside the enum is refused by the table.

    The API already 422s these; this is the guarantee for every other writer,
    the import script included.
    """

    base = (
        "insert into run_feedback (run_id, agent_id, rating, status) values ('r', 'a', $1, $2)"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await config_database.execute(base, 6, "approved")
    with pytest.raises(asyncpg.CheckViolationError):
        await config_database.execute(base, 0, "approved")
    with pytest.raises(asyncpg.CheckViolationError):
        await config_database.execute(base, 3, "meh")
    # Half an engine reference is refused: the pair comes together or not at all.
    with pytest.raises(asyncpg.CheckViolationError):
        await config_database.execute(
            "insert into run_feedback (run_id, agent_id, rating, status, engine_prompt_id) "
            "values ('r', 'a', 3, 'approved', 'x-draft')"
        )


async def test_the_migration_is_in_the_ledger(config_database: ConfigDatabase) -> None:
    applied = await config_database.fetchval(
        "select count(*) from schema_migrations where filename = '0006_run_feedback.sql'"
    )
    assert applied == 1


# --- Bringing the existing collection over ----------------------------------


async def test_the_firestore_collection_imports_once_and_resolves_the_join(
    api: AsyncClient,
    agent: dict[str, Any],
    database: FirestoreDB,
    config_database: ConfigDatabase,
) -> None:
    """S5's shape for S11's data: one-way, idempotent, joinable on arrival.

    A verdict left in the Firestore collection (written before the cutover, or
    by an instance without CONFIG_DB_DSN) comes over with its own id kept as
    ``source_id``, its promotion preserved, and the prompt version resolved the
    same way a fresh verdict's is.
    """

    from datetime import UTC, datetime

    from app.db.firestore import FEEDBACK
    from app.services.feedback_import import FeedbackImporter
    from app.services.runs import RunService

    await publish_engine_prompt(database, "Draft a post.")
    await register_run(api, agent, "run-old", prompt_id=PROMPT_ID, prompt_version=2)

    stale = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    await database.document(FEEDBACK, "fb-old-1").set(
        {
            "run_id": "run-old",
            "agent_id": agent["id"],
            "rating": 4,
            "status": "approved",
            "correction_notes": None,
            "corrected_output": None,
            "reviewer": "lola",
            "tags": ["legacy"],
            "promoted_example_id": "ex-legacy",
            "created_at": stale,
            "updated_at": stale,
        }
    )
    await database.document(FEEDBACK, "fb-orphan").set(
        {
            "run_id": "run-that-was-purged",
            "agent_id": agent["id"],
            "rating": 1,
            "status": "rejected",
            "reviewer": "shlomi",
            "tags": [],
            "created_at": stale,
            "updated_at": stale,
        }
    )

    importer = FeedbackImporter(database, config_database, RunService(database))

    dry = await importer.run(dry_run=True)
    assert dry.counts() == {"imported": 2, "already_present": 0, "refused": 0}
    assert await config_database.fetchval("select count(*) from run_feedback") == 0

    first = await importer.run()
    assert first.counts() == {"imported": 2, "already_present": 0, "refused": 0}
    second = await importer.run()
    assert second.counts() == {"imported": 0, "already_present": 2, "refused": 0}

    rows = {
        r["source_id"]: r
        for r in await config_database.fetch(
            "select source_id, run_id, rating, promoted_example_id, tags, created_at, "
            "engine_prompt_id, client_slug from run_feedback"
        )
    }
    assert set(rows) == {"fb-old-1", "fb-orphan"}
    old = rows["fb-old-1"]
    assert (old["run_id"], old["rating"], old["promoted_example_id"]) == ("run-old", 4, "ex-legacy")
    assert list(old["tags"]) == ["legacy"]
    assert old["created_at"] == stale
    assert (old["engine_prompt_id"], old["client_slug"]) == (PROMPT_ID, "acme")
    # The orphan is history too: kept with what its document said, nothing resolved.
    orphan = rows["fb-orphan"]
    assert (orphan["run_id"], orphan["engine_prompt_id"], orphan["client_slug"]) == (
        "run-that-was-purged", None, None,
    )

    # And the imported verdicts are the ones the API now serves.
    listed = (await api.get(f"/agents/{agent['id']}/runs/run-old/feedback")).json()
    assert [item["reviewer"] for item in listed] == ["lola"]
    assert listed[0]["promoted_example_id"] == "ex-legacy"
