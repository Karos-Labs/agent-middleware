"""One prompt store, with the engine's documents as a projection of it.

S7 / SCRUM-221. What these pin is the difference between the endpoint Studio
has been writing to since 24.08 and its replacement:

    before   PUT replaces content IN PLACE, pushes the old text onto a
             `supersededHistory` array capped at TEN entries, drops the
             eleventh off the end, and offers no way to read one entry back or
             put one back.

    after    every version kept forever in `config.prompt_versions`
             (append-only by trigger), the current one PROJECTED into the
             document the pinned skillRef names, and a restore that is a new
             version rather than a mutation.

The projection is the part that makes it safe to ship: agent-engine reads
`promptVersions/{id}@{v}.content` exactly as before, so no run path changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.db.firestore import FirestoreDB
from app.db.postgres import ConfigDatabase
from app.main import build_services, create_app
from app.services.engine_prompts import (
    ENGINE_PROMPT_VERSIONS,
    ENGINE_PROMPTS,
    HISTORY_LIMIT,
)
from app.services.prompt_store import UnifiedPromptStore, prompt_key_for
from tests.conftest_postgres import requires_postgres

pytestmark = requires_postgres

PROMPT_ID = "x-draft"
ENGINE_VERSION = "2"


async def publish_engine_prompt(
    firestore: FirestoreDB, content: str, *, history: list[dict[str, Any]] | None = None
) -> None:
    """The two documents agent-engine's FirestorePromptStore actually reads."""

    await firestore.document(ENGINE_PROMPTS, PROMPT_ID).set(
        {
            "latestVersion": ENGINE_VERSION,
            **({"supersededHistory": history} if history is not None else {}),
        }
    )
    await firestore.document(
        ENGINE_PROMPT_VERSIONS, f"{PROMPT_ID}@{ENGINE_VERSION}"
    ).set({"content": content, "updated_by": "someone@karoslabs.com"})


@pytest.fixture
def store(config_database: ConfigDatabase, database: FirestoreDB) -> UnifiedPromptStore:
    return UnifiedPromptStore(config_database, database)


@pytest.fixture
async def api(
    settings: Any,
    database: FirestoreDB,
    publisher_service: Any,
    config_database: ConfigDatabase,
) -> AsyncIterator[AsyncClient]:
    """The app with the store wired in.

    httpx rather than TestClient for the same reason S4's suite uses it:
    TestClient runs the app on its own event loop, and the asyncpg pool belongs
    to the test's. Sharing a connection across loops shows up as an unrelated
    timeout three tests later.
    """

    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app, settings, database, publisher=publisher_service,
            config_database=config_database,
        )
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            yield client


async def live_content(firestore: FirestoreDB) -> str:
    snapshot = await firestore.document(
        ENGINE_PROMPT_VERSIONS, f"{PROMPT_ID}@{ENGINE_VERSION}"
    ).get()
    return str((snapshot.to_dict() or {}).get("content"))


# --- The projection ---------------------------------------------------------


async def test_a_save_writes_a_version_here_and_projects_it_there(
    store: UnifiedPromptStore, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    """The whole design in one test.

    The engine reads `promptVersions/{id}@{v}.content`. If a save did not write
    there, every Studio edit would be inert -- which is what "just create a new
    version instead of overwriting" would have produced, because a stage's
    skillRef is pinned in compiled TypeScript.
    """

    await publish_engine_prompt(database, "Draft a post.")

    saved = await store.save(
        PROMPT_ID, ENGINE_VERSION, "Draft a warmer post.", actor="shlomi@karoslabs.com"
    )

    assert saved["content"] == "Draft a warmer post."
    assert saved["is_live"] is True
    # There, so the next run picks it up with no engine change.
    assert await live_content(database) == "Draft a warmer post."
    # And here, immutably.
    rows = await config_database.fetch(
        "select version, content, origin from prompt_versions pv "
        "join prompts p on p.id = pv.prompt_id where p.prompt_key = $1 order by version",
        prompt_key_for(PROMPT_ID, ENGINE_VERSION),
    )
    assert [(r["version"], r["origin"]) for r in rows] == [(1, "imported"), (2, "authored")]
    assert rows[0]["content"] == "Draft a post."


async def test_the_projected_document_records_which_version_produced_it(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    # So somebody reading the Firestore document can find the version behind it
    # without guessing.
    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")

    snapshot = await database.document(
        ENGINE_PROMPT_VERSIONS, f"{PROMPT_ID}@{ENGINE_VERSION}"
    ).get()
    assert (snapshot.to_dict() or {})["promptStoreVersion"] == 2


# --- The import, which is the part that must lose nothing -------------------


async def test_the_first_save_imports_the_history_that_already_exists(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    """Without this, the fix for losing prompt text would begin by losing all of it.

    Order is the opposite of intuition: `supersededHistory` holds text that WAS
    live and was replaced, oldest first, and the document's current `content`
    is what is live NOW. So the array becomes the early versions and the live
    text becomes the latest.
    """

    await publish_engine_prompt(
        database,
        "The third thing.",
        history=[
            {"version": ENGINE_VERSION, "content": "The first thing.", "replaced_by": "a@b.com"},
            {"version": ENGINE_VERSION, "content": "The second thing.", "replaced_by": "a@b.com"},
        ],
    )

    await store.save(PROMPT_ID, ENGINE_VERSION, "The fourth thing.", actor="c@d.com")
    versions = await store.versions(PROMPT_ID, ENGINE_VERSION)

    assert [(v["version"], v["content"]) for v in reversed(versions)] == [
        (1, "The first thing."),
        (2, "The second thing."),
        (3, "The third thing."),
        (4, "The fourth thing."),
    ]
    assert [v["origin"] for v in reversed(versions)] == [
        "imported",
        "imported",
        "imported",
        "authored",
    ]


async def test_an_imported_version_is_marked_as_one(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    """Because an import is a FLOOR on the history, not the whole of it.

    The array was capped at ten, so an agent edited fifteen times has lost its
    first five edits and nothing here can recover them. A reader who could not
    tell an imported version from an authored one would read this as a complete
    record and conclude the prompt had only ever been what it shows.
    """

    await publish_engine_prompt(
        database,
        "Live.",
        history=[
            {"version": ENGINE_VERSION, "content": f"Edit {n}.", "replaced_by": "a@b.com"}
            for n in range(HISTORY_LIMIT)
        ],
    )

    await store.save(PROMPT_ID, ENGINE_VERSION, "New.", actor="c@d.com")
    versions = await store.versions(PROMPT_ID, ENGINE_VERSION)

    imported = [v for v in versions if v["origin"] == "imported"]
    assert len(imported) == HISTORY_LIMIT + 1  # the ten entries plus the live text
    assert all("Recovered from supersededHistory" in (v["notes"] or "") for v in imported[1:])


async def test_history_entries_for_another_engine_version_are_not_claimed(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    # The array is on the PROMPT document and carries entries for every version
    # of it. Importing them all into one version's history would attribute text
    # to a stage that never ran it.
    await publish_engine_prompt(
        database,
        "Live for v2.",
        history=[
            {"version": "1", "content": "Belongs to v1.", "replaced_by": "a@b.com"},
            {"version": ENGINE_VERSION, "content": "Belongs to v2.", "replaced_by": "a@b.com"},
        ],
    )

    await store.save(PROMPT_ID, ENGINE_VERSION, "New.", actor="c@d.com")
    contents = [v["content"] for v in await store.versions(PROMPT_ID, ENGINE_VERSION)]

    assert "Belongs to v1." not in contents
    assert "Belongs to v2." in contents


async def test_the_import_happens_once(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    await publish_engine_prompt(
        database,
        "Live.",
        history=[{"version": ENGINE_VERSION, "content": "Old.", "replaced_by": "a@b.com"}],
    )

    await store.save(PROMPT_ID, ENGINE_VERSION, "One.", actor="a@b.com")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Two.", actor="a@b.com")

    versions = await store.versions(PROMPT_ID, ENGINE_VERSION)
    assert [v["origin"] for v in reversed(versions)] == [
        "imported",
        "imported",
        "authored",
        "authored",
    ]


# --- Immutability -----------------------------------------------------------


async def test_a_version_cannot_be_overwritten_even_directly(
    store: UnifiedPromptStore, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    """The rule is the database's, not the service's.

    An application-level "we never update this" survives exactly as long as the
    next refactor, and the refactor that reintroduces it is the one that looks
    like a performance improvement.
    """

    import asyncpg

    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")

    with pytest.raises(asyncpg.exceptions.RestrictViolationError):
        await config_database.execute(
            "update prompt_versions set content = 'overwritten' where version = 1"
        )
    with pytest.raises(asyncpg.exceptions.RestrictViolationError):
        await config_database.execute("delete from prompt_versions where version = 1")


async def test_saving_identical_content_does_not_write_a_version(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    # A save that changes nothing should not add a row a reviewer then has to
    # diff against its identical predecessor.
    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")
    before = await store.versions(PROMPT_ID, ENGINE_VERSION)

    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")
    after = await store.versions(PROMPT_ID, ENGINE_VERSION)

    assert len(after) == len(before)


async def test_an_empty_save_is_refused(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    """A stage with no system prompt does not fail -- it runs on nothing.

    It produces plausible, unmoored output, which is worse than an error. So it
    is an error.
    """

    from app.core.exceptions import InvalidStateError

    await publish_engine_prompt(database, "Draft a post.")
    with pytest.raises(InvalidStateError):
        await store.save(PROMPT_ID, ENGINE_VERSION, "   \n  ", actor="a@b.com")


# --- Restore ----------------------------------------------------------------


async def test_restore_is_a_new_version_and_updates_what_runs(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    """The path the old endpoint never had.

    Additive on purpose: a restore that mutated history would be the same
    destructive edit wearing a different name, and "was this text ever live,
    and when" would stop having an answer.
    """

    await publish_engine_prompt(database, "The good one.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "The bad edit.", actor="a@b.com")

    restored = await store.restore(
        PROMPT_ID, ENGINE_VERSION, 1, actor="b@c.com", reason="the edit was off-brand"
    )

    assert restored["version"] == 3
    assert restored["content"] == "The good one."
    assert restored["origin"] == "restored"
    assert restored["restored_from_version"] == 1
    assert restored["is_live"] is True
    # The bad edit is still in the record. That is the point.
    contents = {v["version"]: v["content"] for v in await store.versions(PROMPT_ID, ENGINE_VERSION)}
    assert contents == {1: "The good one.", 2: "The bad edit.", 3: "The good one."}
    # And the engine is running the restored text.
    assert await live_content(database) == "The good one."


async def test_restoring_the_live_version_is_refused(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    # It would write a version identical to the one running and record a change
    # that did not happen.
    from app.core.exceptions import InvalidStateError

    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")

    with pytest.raises(InvalidStateError) as raised:
        await store.restore(PROMPT_ID, ENGINE_VERSION, 2, actor="a@b.com")
    assert "already the live one" in str(raised.value)


async def test_restoring_a_version_that_does_not_exist_is_404(
    store: UnifiedPromptStore, database: FirestoreDB
) -> None:
    from app.core.exceptions import ResourceNotFoundError

    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")

    with pytest.raises(ResourceNotFoundError):
        await store.restore(PROMPT_ID, ENGINE_VERSION, 99, actor="a@b.com")


# --- Audit ------------------------------------------------------------------


async def test_every_save_and_restore_leaves_an_audit_row(
    store: UnifiedPromptStore, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    await publish_engine_prompt(database, "Draft a post.")
    await store.save(PROMPT_ID, ENGINE_VERSION, "Second.", actor="a@b.com")
    await store.restore(PROMPT_ID, ENGINE_VERSION, 1, actor="b@c.com", reason="reverting")

    rows = await config_database.fetch(
        "select actor, action, entity_type, entity_id, after, note from audit_log "
        "order by id"
    )
    assert [(r["actor"], r["action"]) for r in rows] == [
        ("a@b.com", "update"),
        ("b@c.com", "restore"),
    ]
    assert all(r["entity_type"] == "prompt_version" for r in rows)
    assert rows[0]["entity_id"] == prompt_key_for(PROMPT_ID, ENGINE_VERSION)
    # Dicts, not strings: the jsonb codec already serialises, and pre-dumping
    # stored a JSON string containing JSON.
    assert rows[1]["after"]["restored_from"] == 1
    assert rows[1]["note"] == "reverting"


# --- The HTTP surface -------------------------------------------------------


async def test_the_legacy_put_now_writes_through_the_store(
    api: AsyncClient, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    """Studio's existing call, unchanged, with history that no longer disappears.

    The route, the body and the response are the same. What changed is that the
    superseded text goes somewhere uncapped instead of onto the end of a
    ten-entry array.
    """

    await publish_engine_prompt(database, "Draft a post.")

    response = await api.put(
        f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}",
        json={"content": "Draft a warmer post."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Draft a warmer post."

    listed = await api.get(f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}/store")
    assert listed.status_code == 200, listed.text
    assert [v["version"] for v in listed.json()] == [2, 1]

    one = await api.get(f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}/store/1")
    assert one.status_code == 200
    assert one.json()["content"] == "Draft a post."


async def test_restore_over_http_reinstates_and_reports(
    api: AsyncClient, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    await publish_engine_prompt(database, "The good one.")
    await api.put(
        f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}",
        json={"content": "The bad edit."},
    )

    restored = await api.post(
        f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}/store/1/restore",
        json={"reason": "off-brand"},
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["content"] == "The good one."
    assert restored.json()["restored_from_version"] == 1
    assert await live_content(database) == "The good one."


async def test_the_store_routes_report_503_without_a_configuration_database(
    settings: Any, database: FirestoreDB, publisher_service: Any
) -> None:
    """And the legacy PUT still works, which is the whole point of keeping it.

    A Studio edit that stops working because Cloud SQL has not been stood up
    yet is a worse regression than a capped history -- and it is exactly what
    "just stop overwriting" would have caused.
    """

    from contextlib import asynccontextmanager

    from app.main import build_services, create_app

    app = create_app()

    @asynccontextmanager
    async def lifespan(_: Any):  # type: ignore[no-untyped-def]
        build_services(app, settings, database, publisher=publisher_service)
        yield

    app.router.lifespan_context = lifespan
    await publish_engine_prompt(database, "Draft a post.")

    with TestClient(app) as legacy:
        listed = legacy.get(f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}/store")
        assert listed.status_code == 503
        assert "CONFIG_DB_DSN" in listed.json()["detail"]

        written = legacy.put(
            f"/engine-prompts/{PROMPT_ID}/versions/{ENGINE_VERSION}",
            json={"content": "Still editable."},
        )
        assert written.status_code == 200, written.text
        assert await live_content(database) == "Still editable."
