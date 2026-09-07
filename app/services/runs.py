"""Run registry: one document per job handed to the engine.

Runs live in a root collection rather than under the agent, because a run id is
globally unique (the portal may mint it) and because feedback lookups address a
run by id alone. Listing runs for an agent is therefore an indexed query -- see
``firestore.indexes.json``.

A run freezes which prompt and template version produced it, which is what lets
feedback be attributed to a specific version long after the agent has moved on.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from app.api.schemas.run import RunCreate, RunUpdate
from app.core.enums import TERMINAL_RUN_STATUSES, RunStatus
from app.core.exceptions import ResourceConflictError, ResourceNotFoundError
from app.db.firestore import RUNS, FirestoreDB, snapshot_to_dict, utcnow

logger = logging.getLogger(__name__)


class RunService:
    """Creates, updates and lists agent runs."""

    def __init__(self, db: FirestoreDB) -> None:
        self._db = db

    async def register(self, agent_id: str, payload: RunCreate) -> dict[str, Any]:
        """Record a run the caller has dispatched (or is about to).

        Used by portals that build and publish the payload themselves: the
        middleware only needs to know the run exists so feedback can be attached
        to it later.
        """

        return await self.create(
            agent_id,
            run_id=payload.run_id,
            status=payload.status,
            job_type=payload.job_type,
            prompt_id=payload.prompt_id,
            prompt_version=payload.prompt_version,
            template_version_id=payload.template_version_id,
            input_payload=payload.input_payload,
            pubsub_message_id=payload.pubsub_message_id,
            requested_by=payload.requested_by,
            client_slug=payload.client_slug,
        )

    async def create(
        self,
        agent_id: str,
        *,
        run_id: str | None = None,
        status: RunStatus = RunStatus.PENDING,
        job_type: str | None = None,
        prompt_id: str | None = None,
        prompt_version: int | None = None,
        template_version_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
        pubsub_message_id: str | None = None,
        requested_by: str | None = None,
        client_slug: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        now = utcnow()
        document = {
            "agent_id": agent_id,
            "status": status.value,
            "job_type": job_type,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "template_version_id": template_version_id,
            "input_payload": input_payload or {},
            "output": None,
            "error": None,
            "pubsub_message_id": pubsub_message_id,
            "requested_by": requested_by,
            "client_slug": client_slug,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            await self._db.document(RUNS, run_id).create(document)
        except AlreadyExists as exc:
            raise ResourceConflictError(f"run '{run_id}' already exists") from exc

        return {**document, "id": run_id}

    async def get(self, run_id: str, *, agent_id: str | None = None) -> dict[str, Any]:
        """Fetch a run, optionally asserting it belongs to ``agent_id``."""

        snapshot = await self._db.document(RUNS, run_id).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("run", run_id)

        run = snapshot_to_dict(snapshot)
        if agent_id is not None and run.get("agent_id") != agent_id:
            # Do not leak the existence of another agent's run.
            raise ResourceNotFoundError("run", run_id)
        return run

    async def update(self, agent_id: str, run_id: str, payload: RunUpdate) -> dict[str, Any]:
        """Apply an engine callback: status, produced artifact or error."""

        run = await self.get(run_id, agent_id=agent_id)

        patch: dict[str, Any] = {}
        for field, value in payload.model_dump(exclude_unset=True).items():
            patch[field] = value.value if isinstance(value, RunStatus) else value

        if not patch:
            return run

        new_status = patch.get("status")
        if new_status in {status.value for status in TERMINAL_RUN_STATUSES}:
            patch.setdefault("completed_at", utcnow())

        patch["updated_at"] = utcnow()
        await self._db.document(RUNS, run_id).update(patch)
        return {**run, **patch}

    async def mark_dispatched(self, run_id: str, pubsub_message_id: str) -> None:
        await self._db.document(RUNS, run_id).update(
            {
                "status": RunStatus.DISPATCHED.value,
                "pubsub_message_id": pubsub_message_id,
                "updated_at": utcnow(),
            }
        )

    async def update_failure(self, run_id: str, error: str) -> None:
        """Mark a run failed, e.g. when its message could not be published."""

        now = utcnow()
        await self._db.document(RUNS, run_id).update(
            {
                "status": RunStatus.FAILED.value,
                "error": error,
                "completed_at": now,
                "updated_at": now,
            }
        )

    async def list_for_client(
        self,
        client_slug: str,
        *,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every run for one tenant, across all agents, newest first.

        The query the ticket exists for. Attributing a run to a client after the
        fact was impossible not because the listing was missing but because the
        field was: this is the half that makes the stored value useful, and
        without it `client_slug` would be a column nobody can search.

        Deliberately NOT agent-scoped. "Which runs did we do for this client
        this month, and what did they cost" is a question about a tenant, and
        answering it by looping thirteen agent-scoped listings is thirteen
        queries and a merge in the caller.
        """

        query = self._db.collection(RUNS).where(
            filter=FieldFilter("client_slug", "==", client_slug)
        )
        if status is not None:
            query = query.where(filter=FieldFilter("status", "==", status.value))

        query = query.order_by("created_at", direction="DESCENDING")
        if offset:
            query = query.offset(offset)

        runs = [snapshot_to_dict(snapshot) async for snapshot in query.limit(limit + 1).stream()]
        return runs[:limit], len(runs) > limit

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        status: RunStatus | None = None,
        client_slug: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Runs of an agent, newest first, plus whether more pages follow.

        Runs are the one high-volume collection here, so this is a real indexed
        Firestore query rather than an in-process filter.
        """

        query = self._db.collection(RUNS).where(filter=FieldFilter("agent_id", "==", agent_id))
        if status is not None:
            query = query.where(filter=FieldFilter("status", "==", status.value))
        if client_slug is not None:
            query = query.where(filter=FieldFilter("client_slug", "==", client_slug))

        query = query.order_by("created_at", direction="DESCENDING")
        if offset:
            query = query.offset(offset)

        # One extra document tells us whether another page exists, without a count.
        runs = [snapshot_to_dict(snapshot) async for snapshot in query.limit(limit + 1).stream()]
        return runs[:limit], len(runs) > limit
