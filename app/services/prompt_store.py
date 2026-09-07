"""One prompt store, with the engine's documents as a projection of it.

SCRUM-221 / S7. A prompt lives in six places today:

1. ``promptVersions/{id}@{v}`` -- what the engine actually loads.
2. ``prompts/{id}.supersededHistory`` -- an array of displaced text, CAPPED AT
   TEN ENTRIES, with no way to read a single entry back and no restore path.
3. ``agents/{slug}/prompts/{NNNNNN}`` -- this service's own versioned system
   prompts, which the engine has never read.
4. The engine's Vertex-backed ``PromptStore``.
5. The engine's file-backed ``PromptStore``.
6. ``AgentDefinition.stages[].systemPrompt`` -- inline on a dynamic agent spec.

The worst of the six is the second, because it is the one Studio has been
writing to since 24.08: ``PUT /engine-prompts/{id}/versions/{v}`` replaces
content IN PLACE, pushes the old text onto that array, and drops the eleventh
entry off the end.

## The overwrite is not laziness, which is why "stop overwriting" is not the fix

A stage's ``skillRef`` is pinned in compiled TypeScript -- ``skillRef:
"x-draft@2"`` -- so the engine loads exactly that document and no other.
Creating a ``@3`` would be **inert**: nothing loads it until somebody changes
TypeScript and redeploys. So an in-place write is the only write that takes
effect, and removing it would make every Studio edit silently do nothing --
which is a worse bug than the one being fixed.

## So: authority here, projection there

Every version is kept HERE, in ``config.prompt_versions``, append-only by
trigger and uncapped. The current one is written THERE, into the document the
pinned skillRef names. The engine sees exactly what it saw before, and the run
path does not change at all.

That is also what makes this reversible -- the same argument S5 makes for its
import. Delete these rows and the Firestore documents are still the source of
truth.

## Two-phase, in an order chosen for what survives a failure

A Postgres transaction and a Firestore write cannot be one transaction, so the
order decides what a half-failure leaves behind:

1. Record the new version in Postgres. Committed before anything else, so the
   text a human wrote is never the thing that gets lost.
2. Write it into the engine's document.
3. Record in Postgres that step 2 happened.

If step 2 fails, the version exists and ``prompts.projected_version`` lags
behind ``max(version)`` -- which is exactly what that column is for. The
alternative order loses the edit on a Firestore hiccup, and the edit is the
only part a person cannot reproduce.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import asyncpg

from app.core.exceptions import InvalidStateError, ResourceNotFoundError
from app.db.firestore import FirestoreDB, utcnow
from app.db.postgres import ConfigDatabase
from app.services.engine_prompts import (
    ENGINE_PROMPT_VERSIONS,
    ENGINE_PROMPTS,
    HISTORY_LIMIT,
)

logger = logging.getLogger(__name__)


def prompt_key_for(engine_prompt_id: str, engine_version: str) -> str:
    """The store's key for an engine prompt.

    ``engine/x-draft/2`` rather than ``x-draft@2``, because ``config.prompts``
    constrains ``prompt_key`` to a URL-safe charset that excludes ``@`` -- and
    a key that cannot appear in a path is a key somebody has to escape at every
    call site.
    """

    return f"engine/{engine_prompt_id}/{engine_version}"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class UnifiedPromptStore:
    """The append-only prompt store, and its projection into the engine."""

    def __init__(self, config_db: ConfigDatabase, firestore: FirestoreDB) -> None:
        self._db = config_db
        self._firestore = firestore

    # =====================================================================
    # Reading
    # =====================================================================

    async def versions(
        self, engine_prompt_id: str, engine_version: str
    ) -> list[dict[str, Any]]:
        """Every version, newest first. Uncapped, which is the point.

        The array this replaces kept ten. An agent whose prompt is tuned weekly
        loses its first quarter of history in a quarter, and the entry nobody
        can read back is always the one somebody wants.
        """

        key = prompt_key_for(engine_prompt_id, engine_version)
        rows = await self._db.fetch(
            """
            select pv.version, pv.content, pv.content_hash, pv.notes, pv.origin,
                   pv.created_at, pv.created_by,
                   restored.version as restored_from_version,
                   p.projected_version
              from prompt_versions pv
              join prompts p on p.id = pv.prompt_id
              left join prompt_versions restored on restored.id = pv.restored_from_version_id
             where p.prompt_key = $1
             order by pv.version desc
            """,
            key,
        )
        if not rows:
            raise ResourceNotFoundError(
                "prompt", f"{engine_prompt_id}@{engine_version} (nothing recorded here yet)"
            )
        return [
            {**dict(row), "is_live": row["version"] == row["projected_version"]}
            for row in rows
        ]

    async def version(
        self, engine_prompt_id: str, engine_version: str, version: int
    ) -> dict[str, Any]:
        """One exact version. What the capped array could never give back."""

        key = prompt_key_for(engine_prompt_id, engine_version)
        row = await self._db.fetchrow(
            """
            select pv.version, pv.content, pv.content_hash, pv.notes, pv.origin,
                   pv.created_at, pv.created_by, p.projected_version,
                   restored.version as restored_from_version
              from prompt_versions pv
              join prompts p on p.id = pv.prompt_id
              left join prompt_versions restored on restored.id = pv.restored_from_version_id
             where p.prompt_key = $1 and pv.version = $2
            """,
            key,
            version,
        )
        if row is None:
            raise ResourceNotFoundError(
                "prompt version", f"{engine_prompt_id}@{engine_version} v{version}"
            )
        return {**dict(row), "is_live": row["version"] == row["projected_version"]}

    # =====================================================================
    # Writing
    # =====================================================================

    async def save(
        self,
        engine_prompt_id: str,
        engine_version: str,
        content: str,
        *,
        actor: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """A new immutable version, then the projection that makes it run."""

        if not content.strip():
            raise InvalidStateError(
                "a prompt cannot be saved empty — a stage with no system prompt still "
                "runs, on nothing but its turn contract, and produces plausible "
                "unmoored output. That is worse than an error, so it is an error."
            )

        async with self._db.transaction() as connection:
            prompt_id = await self._ensure_prompt(
                connection, engine_prompt_id, engine_version, actor=actor
            )
            current = await connection.fetchval(
                "select content from prompt_versions where prompt_id = $1 "
                "order by version desc limit 1",
                prompt_id,
            )
            if current == content:
                # Not an error, and not a new version either: a save that
                # changes nothing should not add a row a reviewer then has to
                # diff against its identical predecessor.
                logger.info(
                    "prompt %s@%s saved unchanged; no version written",
                    engine_prompt_id,
                    engine_version,
                )
                return await self._live(engine_prompt_id, engine_version)

            version = await self._insert_version(
                connection, prompt_id, content, origin="authored", actor=actor, notes=notes
            )
            await self._audit(
                connection,
                actor=actor,
                action="update",
                entity_id=prompt_key_for(engine_prompt_id, engine_version),
                after={"version": version, "content_hash": content_hash(content)},
                note=notes,
            )

        await self._project(engine_prompt_id, engine_version, content, version, actor=actor)
        return await self.version(engine_prompt_id, engine_version, version)

    async def restore(
        self,
        engine_prompt_id: str,
        engine_version: str,
        version: int,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Reinstate an earlier version as a NEW one.

        The restore path the old endpoint never had. Deliberately additive: a
        restore that mutated history would be the same destructive edit wearing
        a different name, and the question "was this text ever live, and when"
        would stop having an answer.
        """

        source = await self.version(engine_prompt_id, engine_version, version)
        key = prompt_key_for(engine_prompt_id, engine_version)

        async with self._db.transaction() as connection:
            prompt_id = await connection.fetchval(
                "select id from prompts where prompt_key = $1", key
            )
            live = await connection.fetchval(
                "select projected_version from prompts where id = $1", prompt_id
            )
            if live == version:
                raise InvalidStateError(
                    f"version {version} of {engine_prompt_id}@{engine_version} is already "
                    "the live one; restoring it would write a version identical to the "
                    "one running and record a change that did not happen"
                )

            source_id = await connection.fetchval(
                "select id from prompt_versions where prompt_id = $1 and version = $2",
                prompt_id,
                version,
            )
            new_version = await self._insert_version(
                connection,
                prompt_id,
                source["content"],
                origin="restored",
                actor=actor,
                notes=reason or f"Restored version {version}.",
                restored_from=source_id,
            )
            await self._audit(
                connection,
                actor=actor,
                action="restore",
                entity_id=key,
                before={"version": live},
                after={"version": new_version, "restored_from": version},
                note=reason,
            )

        await self._project(
            engine_prompt_id, engine_version, source["content"], new_version, actor=actor
        )
        logger.info(
            "restored prompt %s@%s to the content of version %s as version %s",
            engine_prompt_id,
            engine_version,
            version,
            new_version,
        )
        return await self.version(engine_prompt_id, engine_version, new_version)

    # =====================================================================
    # Internals
    # =====================================================================

    async def _ensure_prompt(
        self,
        connection: asyncpg.Connection,
        engine_prompt_id: str,
        engine_version: str,
        *,
        actor: str,
    ) -> Any:
        """The prompt row, importing what already exists the first time.

        The import is the part that matters. Without it, the first save under
        this scheme starts the history at "version 1" and orphans everything
        the old endpoint had accumulated -- so the change that was supposed to
        stop losing prompt text would begin by losing all of it.
        """

        key = prompt_key_for(engine_prompt_id, engine_version)
        existing = await connection.fetchval("select id from prompts where prompt_key = $1", key)
        if existing is not None:
            return existing

        prompt_id = await connection.fetchval(
            """
            insert into prompts (
                prompt_key, purpose, description, engine_prompt_id, engine_version,
                source_registry, source_id, imported_at, created_by
            ) values ($1, 'skill', $2, $3, $4, 'engine_prompts', $5, now(), $6)
            returning id
            """,
            key,
            f"agent-engine skillRef {engine_prompt_id}@{engine_version}",
            engine_prompt_id,
            engine_version,
            f"promptVersions/{engine_prompt_id}@{engine_version}",
            actor,
        )
        await self._import_legacy(connection, prompt_id, engine_prompt_id, engine_version)
        return prompt_id

    async def _import_legacy(
        self,
        connection: asyncpg.Connection,
        prompt_id: Any,
        engine_prompt_id: str,
        engine_version: str,
    ) -> None:
        """Recover the old endpoint's history: the array first, then what is live.

        Order matters and is the opposite of intuition. ``supersededHistory``
        holds text that WAS live and was replaced, oldest first; the document's
        current ``content`` is what is live NOW. So the array becomes the early
        versions and the live text becomes the latest one.

        Everything imported is marked ``origin = 'imported'``, because this is
        a FLOOR on the history and not the whole of it: the array was capped at
        ten, so an agent edited fifteen times has lost its first five edits and
        nothing here can recover them. A reader who cannot tell an imported
        version from an authored one would read this as a complete record.
        """

        history_doc = await self._firestore.document(ENGINE_PROMPTS, engine_prompt_id).get()
        entries: list[Any] = []
        if history_doc.exists:
            raw = (history_doc.to_dict() or {}).get("supersededHistory")
            if isinstance(raw, list):
                entries = [
                    entry
                    for entry in raw
                    if isinstance(entry, dict)
                    and str(entry.get("version")) == engine_version
                    and isinstance(entry.get("content"), str)
                    and entry["content"].strip()
                ]

        imported = 0
        for entry in entries:
            await self._insert_version(
                connection,
                prompt_id,
                entry["content"],
                origin="imported",
                actor=entry.get("replaced_by"),
                notes="Recovered from supersededHistory.",
            )
            imported += 1

        doc_id = f"{engine_prompt_id}@{engine_version}"
        live_doc = await self._firestore.document(ENGINE_PROMPT_VERSIONS, doc_id).get()
        if live_doc.exists:
            live = (live_doc.to_dict() or {}).get("content")
            if isinstance(live, str) and live.strip():
                version = await self._insert_version(
                    connection,
                    prompt_id,
                    live,
                    origin="imported",
                    actor=(live_doc.to_dict() or {}).get("updated_by"),
                    notes="The text that was live when this prompt was imported.",
                )
                await connection.execute(
                    "update prompts set projected_version = $2, projected_at = now() "
                    "where id = $1",
                    prompt_id,
                    version,
                )
                imported += 1

        if imported:
            logger.info(
                "imported %d version(s) of %s@%s into the prompt store%s",
                imported,
                engine_prompt_id,
                engine_version,
                (
                    f" (supersededHistory was capped at {HISTORY_LIMIT}, so anything "
                    "older than that is not recoverable)"
                    if len(entries) >= HISTORY_LIMIT
                    else ""
                ),
            )

    async def _insert_version(
        self,
        connection: asyncpg.Connection,
        prompt_id: Any,
        content: str,
        *,
        origin: str,
        actor: str | None,
        notes: str | None = None,
        restored_from: Any = None,
    ) -> int:
        next_version = await connection.fetchval(
            "select coalesce(max(version), 0) + 1 from prompt_versions where prompt_id = $1",
            prompt_id,
        )
        await connection.execute(
            """
            insert into prompt_versions (
                prompt_id, version, content, content_hash, notes, origin,
                restored_from_version_id, created_by
            ) values ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            prompt_id,
            next_version,
            content,
            content_hash(content),
            notes,
            origin,
            restored_from,
            actor,
        )
        await connection.execute(
            "update prompts set active_version_id = ("
            "  select id from prompt_versions where prompt_id = $1 and version = $2"
            ") where id = $1",
            prompt_id,
            next_version,
        )
        return int(next_version)

    async def _project(
        self,
        engine_prompt_id: str,
        engine_version: str,
        content: str,
        version: int,
        *,
        actor: str,
    ) -> None:
        """Write the current text into the document the pinned skillRef names.

        The whole reason the engine needs no change. It reads
        ``promptVersions/{id}@{v}.content`` exactly as before; this is the same
        write the old endpoint made, minus the destruction.

        ``supersededHistory`` is deliberately NOT written any more. Keeping it
        in step would mean maintaining a capped copy of an uncapped record, and
        the copy would be the one somebody read. The existing entries are left
        alone -- they were imported, not consumed, and deleting the evidence
        that this migration happened is not an improvement.
        """

        now = utcnow()
        doc_id = f"{engine_prompt_id}@{engine_version}"
        await self._firestore.document(ENGINE_PROMPT_VERSIONS, doc_id).update(
            {
                "content": content,
                "updated_at": now,
                "updated_by": actor,
                # So somebody reading the Firestore document can find the
                # version that produced it without guessing.
                "promptStoreVersion": version,
            }
        )
        await self._db.execute(
            "update prompts set projected_version = $2, projected_at = now() "
            "where prompt_key = $1",
            prompt_key_for(engine_prompt_id, engine_version),
            version,
        )

    async def _live(self, engine_prompt_id: str, engine_version: str) -> dict[str, Any]:
        row = await self._db.fetchrow(
            "select projected_version from prompts where prompt_key = $1",
            prompt_key_for(engine_prompt_id, engine_version),
        )
        if row is None or row["projected_version"] is None:
            raise ResourceNotFoundError(
                "live prompt version", f"{engine_prompt_id}@{engine_version}"
            )
        return await self.version(engine_prompt_id, engine_version, row["projected_version"])

    @staticmethod
    async def _audit(
        connection: asyncpg.Connection,
        *,
        actor: str,
        action: str,
        entity_id: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        await connection.execute(
            """
            insert into audit_log (actor, action, entity_type, entity_id, before, after, note)
            values ($1, $2, 'prompt_version', $3, $4, $5, $6)
            """,
            actor,
            action,
            entity_id,
            before,
            after,
            note,
        )
