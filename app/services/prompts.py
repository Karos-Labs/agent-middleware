"""Versioned system prompts and few-shot examples for an agent.

Prompt versions are immutable: an edit is a new version. Version numbers are the
document ids (zero padded), so allocating one is a ``create()`` that either wins
or raises ``AlreadyExists`` -- concurrent writers can retry instead of silently
overwriting each other, which a read-then-write counter would allow.
"""

from __future__ import annotations

import logging
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from app.api.schemas.prompt import (
    FewShotExampleCreate,
    FewShotExampleUpdate,
    SystemPromptCreate,
)
from app.core.enums import ExampleSource
from app.core.exceptions import ResourceConflictError, ResourceNotFoundError
from app.db.firestore import (
    AGENTS,
    EXAMPLES,
    PROMPTS,
    FirestoreDB,
    generate_id,
    snapshot_to_dict,
    utcnow,
    version_doc_id,
)

logger = logging.getLogger(__name__)

MAX_VERSION_ALLOCATION_ATTEMPTS = 5


class PromptService:
    """System prompt versions and few-shot examples."""

    def __init__(self, db: FirestoreDB) -> None:
        self._db = db

    # --- System prompts ----------------------------------------------------

    def _prompts(self, agent_id: str) -> Any:
        return self._db.collection(AGENTS, agent_id, PROMPTS)

    async def create_version(
        self, agent_id: str, payload: SystemPromptCreate, created_by: str
    ) -> dict[str, Any]:
        """Append a new prompt version, optionally making it the active one.

        ``created_by`` is passed in rather than read off ``payload`` because it
        is the verified caller. It used to be a field on the request body, so a
        prompt could name anyone at all as its author.
        """

        for _ in range(MAX_VERSION_ALLOCATION_ATTEMPTS):
            version = await self._next_version(agent_id)
            now = utcnow()
            document = {
                "agent_id": agent_id,
                "version": version,
                "content": payload.content,
                "notes": payload.notes,
                "variables": payload.variables,
                "is_active": payload.activate,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            try:
                await self._prompts(agent_id).document(version_doc_id(version)).create(document)
            except AlreadyExists:
                # Another writer took this version number; recompute and retry.
                continue

            if payload.activate:
                await self._deactivate_others(agent_id, keep_version=version)

            logger.info("Created prompt v%s for agent %s", version, agent_id)
            return {**document, "id": version_doc_id(version)}

        raise ResourceConflictError(
            f"could not allocate a prompt version for agent {agent_id!r}; please retry"
        )

    async def _next_version(self, agent_id: str) -> int:
        latest = [
            snapshot_to_dict(snapshot)
            async for snapshot in self._prompts(agent_id)
            .order_by("version", direction="DESCENDING")
            .limit(1)
            .stream()
        ]
        return (latest[0]["version"] + 1) if latest else 1

    async def list_versions(self, agent_id: str) -> list[dict[str, Any]]:
        """Every version of the prompt, newest first."""

        return [
            snapshot_to_dict(snapshot)
            async for snapshot in self._prompts(agent_id)
            .order_by("version", direction="DESCENDING")
            .stream()
        ]

    async def get_version(self, agent_id: str, version: int) -> dict[str, Any]:
        snapshot = await self._prompts(agent_id).document(version_doc_id(version)).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("system prompt", f"{agent_id}/v{version}")
        return snapshot_to_dict(snapshot)

    async def find_active(self, agent_id: str) -> dict[str, Any] | None:
        """The prompt version the context endpoint hands out, if any.

        Filtering on ``is_active`` alone needs no composite index. Should a brief
        activation overlap ever leave two documents flagged, the highest version
        wins deterministically.
        """

        active = [
            snapshot_to_dict(snapshot)
            async for snapshot in self._prompts(agent_id)
            .where(filter=FieldFilter("is_active", "==", True))
            .stream()
        ]
        if not active:
            return None
        return max(active, key=lambda prompt: prompt.get("version", 0))

    async def get_active(self, agent_id: str) -> dict[str, Any]:
        prompt = await self.find_active(agent_id)
        if prompt is None:
            raise ResourceNotFoundError("active system prompt", agent_id)
        return prompt

    async def activate(self, agent_id: str, version: int) -> dict[str, Any]:
        """Point the agent at ``version``.

        The target is flagged before the previous one is cleared, so a reader
        never observes an agent with no active prompt.
        """

        prompt = await self.get_version(agent_id, version)
        now = utcnow()
        await self._prompts(agent_id).document(version_doc_id(version)).update(
            {"is_active": True, "updated_at": now}
        )
        await self._deactivate_others(agent_id, keep_version=version)
        return {**prompt, "is_active": True, "updated_at": now}

    async def _deactivate_others(self, agent_id: str, *, keep_version: int) -> None:
        now = utcnow()
        async for snapshot in (
            self._prompts(agent_id).where(filter=FieldFilter("is_active", "==", True)).stream()
        ):
            data = snapshot.to_dict() or {}
            if data.get("version") == keep_version:
                continue
            await snapshot.reference.update({"is_active": False, "updated_at": now})

    # --- Few-shot examples -------------------------------------------------

    def _examples(self, agent_id: str) -> Any:
        return self._db.collection(AGENTS, agent_id, EXAMPLES)

    async def create_example(
        self,
        agent_id: str,
        payload: FewShotExampleCreate,
        *,
        source: ExampleSource = ExampleSource.MANUAL,
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        example_id = generate_id()
        document = {
            "agent_id": agent_id,
            "prompt_id": payload.prompt_id,
            "label": payload.label,
            "user_input": payload.user_input,
            "assistant_output": payload.assistant_output,
            "extra": payload.extra,
            "tags": payload.tags,
            "position": payload.position,
            "is_active": payload.is_active,
            "source": source.value,
            "source_run_id": source_run_id,
            "created_at": now,
            "updated_at": now,
        }
        await self._examples(agent_id).document(example_id).set(document)
        return {**document, "id": example_id}

    async def get_example(self, agent_id: str, example_id: str) -> dict[str, Any]:
        snapshot = await self._examples(agent_id).document(example_id).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("few-shot example", example_id)
        return snapshot_to_dict(snapshot)

    async def update_example(
        self, agent_id: str, example_id: str, payload: FewShotExampleUpdate
    ) -> dict[str, Any]:
        example = await self.get_example(agent_id, example_id)
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return example

        patch["updated_at"] = utcnow()
        await self._examples(agent_id).document(example_id).update(patch)
        return {**example, **patch}

    async def delete_example(self, agent_id: str, example_id: str) -> None:
        await self.get_example(agent_id, example_id)
        await self._examples(agent_id).document(example_id).delete()

    async def list_examples(
        self,
        agent_id: str,
        *,
        active_only: bool = False,
        tag: str | None = None,
        source: ExampleSource | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List examples in prompt order.

        Like agents, an agent's example set is small enough to filter in process.
        """

        examples = [
            snapshot_to_dict(snapshot) async for snapshot in self._examples(agent_id).stream()
        ]
        matches = [
            example
            for example in examples
            if (not active_only or example.get("is_active"))
            and (tag is None or tag in (example.get("tags") or []))
            and (source is None or example.get("source") == source.value)
        ]
        matches.sort(key=_example_sort_key)
        return matches[offset : offset + limit], len(matches)

    async def context_examples(self, agent_id: str, limit: int) -> list[dict[str, Any]]:
        """Active examples, in prompt order, capped at ``limit``."""

        if limit <= 0:
            return []
        examples, _ = await self.list_examples(agent_id, active_only=True, limit=limit)
        return examples


def _example_sort_key(example: dict[str, Any]) -> tuple[int, str]:
    """Order examples by explicit position, then by creation time."""

    created_at = example.get("created_at")
    return (example.get("position") or 0, created_at.isoformat() if created_at else "")
