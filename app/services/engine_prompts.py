"""Read and write the prompt store agent-engine actually executes from.

## Two prompt stores, and why this one

This service already had a ``prompts`` concept: ``agents/{slug}/prompts/{NNNNNN}``,
versioned, promotable, with feedback attached. The engine never reads it. Every
AI stage in every agent-engine workflow loads its system prompt through
``FirestorePromptStore``, which reads two ROOT collections:

    prompts/{promptId}                 -> { latestVersion: "2" }
    promptVersions/{promptId}@{version} -> { content: "..." }

So the Studio could version and promote prompts all day and change nothing
about what an agent ran. This service talks to the collections the engine
reads, so an edit here is an edit to the running system.

## Why an edit updates a version in place

A stage's ``skillRef`` is pinned in the agent's compiled config -- ``skillRef:
"x-draft@2"`` -- so the engine loads exactly that version and no other.
Creating a ``@3`` would be inert: nothing would load it until somebody changed
TypeScript. Requirement is that a Studio edit takes effect on the next run, so
a save writes the version the skillRef names.

That trades away version history, which is a real cost, so the superseded text
is kept on the ``prompts/{promptId}`` document rather than thrown away. The
engine reads only ``latestVersion`` from that document, so extra fields there
are invisible to it -- which is what makes it a safe place to keep an audit
trail.

## Blank content is refused

A save with empty content would leave a stage with no system prompt, and a
BaseAgent with no prompt does not fail -- it runs on its bare turn contract and
produces something plausible and unmoored. That is worse than an error, so it
is an error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.exceptions import InvalidStateError, ResourceNotFoundError
from app.db.firestore import FirestoreDB, utcnow

logger = logging.getLogger(__name__)

#: The engine's own two root collections. Named ENGINE_* to keep them distinct
#: from this service's `agents/{slug}/prompts` subcollection, which shares the
#: word "prompts" and means something else entirely.
ENGINE_PROMPTS = "prompts"
ENGINE_PROMPT_VERSIONS = "promptVersions"

#: How many superseded revisions to keep per prompt. Enough to undo a bad edit
#: or three; not an attempt at being a version-control system.
HISTORY_LIMIT = 10


def split_skill_ref(skill_ref: str) -> tuple[str, str | None]:
    """``"x-draft@2"`` -> ``("x-draft", "2")``; ``"x-draft"`` -> ``("x-draft", None)``.

    An unpinned ref is legal in the engine's own reader (it falls back to
    ``latestVersion``), so it is legal here.
    """
    prompt_id, _, version = skill_ref.partition("@")
    return prompt_id, version or None


class EnginePromptService:
    """The engine's prompt store, as the Studio's read/write surface.

    As of S7 (SCRUM-221) this is a FRONT for the append-only store in
    ``app/services/prompt_store.py`` whenever the configuration database is
    configured: a save writes an immutable version there and projects it here,
    so the engine reads exactly what it read before and the history is no
    longer capped at ten entries.

    The in-place path below stays as the fallback for an environment with no
    Cloud SQL yet (S1 is still open in some), because a Studio edit that stops
    working is worse than a Studio edit whose history is capped -- and it is
    exactly the regression that "just stop overwriting" would have caused.
    """

    def __init__(self, db: FirestoreDB, store: Any | None = None) -> None:
        self._db = db
        self._store = store

    async def _latest_version(self, prompt_id: str) -> str | None:
        snapshot = await self._db.document(ENGINE_PROMPTS, prompt_id).get()
        if not snapshot.exists:
            return None
        value = (snapshot.to_dict() or {}).get("latestVersion")
        return str(value) if value is not None else None

    async def read(self, skill_ref: str) -> dict[str, Any]:
        """The content the engine would load for this skillRef, right now.

        Resolved the same way the engine resolves it, so what the Studio shows
        is what a run would use -- including the fallback to ``latestVersion``
        for an unpinned ref. Reading it any other way would risk an editor that
        displays one prompt while the agent executes another.
        """
        prompt_id, version = split_skill_ref(skill_ref)
        resolved = version or await self._latest_version(prompt_id)
        if resolved is None:
            raise ResourceNotFoundError("engine prompt", prompt_id)

        doc_id = f"{prompt_id}@{resolved}"
        snapshot = await self._db.document(ENGINE_PROMPT_VERSIONS, doc_id).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("engine prompt version", doc_id)

        data = snapshot.to_dict() or {}
        content = data.get("content")
        if not isinstance(content, str):
            raise InvalidStateError(f"promptVersions/{doc_id} has no string 'content' field")

        return {
            "prompt_id": prompt_id,
            "version": resolved,
            "skill_ref": f"{prompt_id}@{resolved}",
            "content": content,
            "pinned": version is not None,
            "updated_at": data.get("updated_at"),
            "updated_by": data.get("updated_by"),
        }

    async def write(self, skill_ref: str, content: str, actor: str | None = None) -> dict[str, Any]:
        """Replaces the content of the version this skillRef names.

        In place, deliberately -- see this module's docstring. The version must
        already exist: creating one the engine has no skillRef pointing at
        would be writing a prompt nothing loads, and silently doing nothing is
        the failure this whole change exists to fix.
        """
        if not content.strip():
            raise InvalidStateError(
                "a prompt cannot be saved empty — a stage with no system prompt still runs, "
                "on nothing but its turn contract, and produces plausible unmoored output"
            )

        prompt_id, version = split_skill_ref(skill_ref)
        resolved = version or await self._latest_version(prompt_id)

        if self._store is not None and resolved is not None:
            # The S7 path: an immutable version, then the projection into the
            # document below. Same effect on the next run, uncapped history,
            # and a restore that exists.
            await self._store.save(prompt_id, resolved, content, actor=actor or "unknown")
            return await self.read(f"{prompt_id}@{resolved}")

        if resolved is None:
            # Publish it from agent-engine first (scripts/publish-prompts.ts).
            raise ResourceNotFoundError("engine prompt", prompt_id)

        doc_id = f"{prompt_id}@{resolved}"
        version_ref = self._db.document(ENGINE_PROMPT_VERSIONS, doc_id)
        snapshot = await version_ref.get()
        if not snapshot.exists:
            # This stage's skillRef names a version that was never published,
            # so there is nothing to edit yet — creating it would write a
            # prompt nothing loads.
            raise ResourceNotFoundError("engine prompt version", doc_id)

        previous = (snapshot.to_dict() or {}).get("content")
        now = utcnow()
        await version_ref.update({"content": content, "updated_at": now, "updated_by": actor})

        # The superseded text, on the document the engine reads only
        # `latestVersion` from — invisible to it, which is what makes it a safe
        # place for an audit trail.
        if isinstance(previous, str) and previous != content:
            await self._append_history(prompt_id, resolved, previous, now, actor)

        logger.warning(
            "Updated engine prompt %s in place (%d chars). No configuration database "
            "is configured, so the superseded text goes to supersededHistory and the "
            "eleventh entry back is lost. Set CONFIG_DB_DSN to keep full history.",
            doc_id,
            len(content),
        )
        return await self.read(f"{prompt_id}@{resolved}")

    async def _append_history(
        self, prompt_id: str, version: str, previous: str, at: Any, actor: str | None
    ) -> None:
        ref = self._db.document(ENGINE_PROMPTS, prompt_id)
        snapshot = await ref.get()
        current = (snapshot.to_dict() or {}) if snapshot.exists else {}
        history = current.get("supersededHistory")
        entries = list(history) if isinstance(history, list) else []
        entries.append(
            {"version": version, "content": previous, "replaced_at": at, "replaced_by": actor}
        )
        await ref.update({"supersededHistory": entries[-HISTORY_LIMIT:]})

    async def history(self, prompt_id: str) -> list[dict[str, Any]]:
        """Superseded revisions, newest last. Empty when nothing has been edited."""
        snapshot = await self._db.document(ENGINE_PROMPTS, prompt_id).get()
        if not snapshot.exists:
            return []
        entries = (snapshot.to_dict() or {}).get("supersededHistory")
        return list(entries) if isinstance(entries, list) else []
