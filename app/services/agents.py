"""Agent registry: create, read, update, enable/disable and logical delete.

The agent document id *is* its slug, so ``POST /agents`` uses
``DocumentReference.create()`` and lets Firestore reject a duplicate atomically
instead of racing a read-then-write uniqueness check.
"""

from __future__ import annotations

import logging
from typing import Any

from google.api_core.exceptions import AlreadyExists

from app.api.schemas.agent import AgentCreate, AgentUpdate
from app.core.enums import AgentStatus
from app.core.exceptions import InvalidStateError, ResourceConflictError, ResourceNotFoundError
from app.db.firestore import AGENTS, MODELS, FirestoreDB, snapshot_to_dict, utcnow

logger = logging.getLogger(__name__)


class AgentService:
    """Reads and writes agent documents."""

    def __init__(self, db: FirestoreDB) -> None:
        self._db = db

    # --- Writes ------------------------------------------------------------

    async def create(self, payload: AgentCreate) -> dict[str, Any]:
        """Persist a new agent from every field ``AgentCreate`` accepts.

        The document is built by dumping the payload rather than by naming
        fields, and that is deliberate. This method used to list eight of them
        by hand and silently drop the other seven -- ``icon``, ``category``,
        ``credit_cost``, ``is_public``, ``required_inputs``, ``stages`` and
        ``stages_read_only``. They reached the catalog only through ``PATCH``
        or the seeder, so an agent created through the API came back from the
        catalog with no icon, no price and no inputs; and because ``AgentRead``
        supplies a default for each one, the 201 response looked complete while
        the stored record was not. A hand-written list is a copy of the schema
        that nothing keeps in step, and it fell out of step.

        ``mode="json"`` unwraps the nested ``AgentInputDef`` / ``AgentStage``
        models and the ``AgentStatus`` enum into the plain JSON types Firestore
        stores, matching the shape ``scripts/seed_all_agents.py`` writes.
        """

        now = utcnow()
        document: dict[str, Any] = {
            **payload.model_dump(mode="json"),
            "deleted_at": None,
            "created_at": now,
            "updated_at": now,
        }

        # Validated BEFORE the write, so a rejected create leaves no row behind.
        # Unreachable at create time until now, which meant an agent could be
        # born naming a model nothing routes and only a later, unrelated edit
        # would surface it.
        await self._reject_unknown_stage_models(document["stages"])

        try:
            await self._db.document(AGENTS, payload.slug).create(document)
        except AlreadyExists as exc:
            raise ResourceConflictError(f"agent '{payload.slug}' already exists") from exc

        logger.info("Created agent %s", payload.slug)
        return {**document, "id": payload.slug}

    async def _reject_unknown_stage_models(self, stages: list[dict[str, Any]] | None) -> None:
        """Refuse a stage naming a model the catalog does not have.

        The whole reason ``models`` is a collection rather than a free-text
        field is that a stage used to be able to name a string nothing routes,
        and nothing found out until a run failed at the model call -- by which
        point the run is a `tooling_error` three layers from the typo. Checked
        here, an unknown id is a 422 on the edit that introduced it.

        A ``retired`` model is still accepted. Retirement exists so a stage that
        already references a model can stop it being SELECTABLE without the
        reference dangling; rejecting it here would break editing any other
        field on an agent whose stage happens to point at one.
        """
        if not stages:
            return
        wanted = {s.get("model_id") for s in stages if s.get("model_id")}
        if not wanted:
            return
        missing: list[str] = []
        for model_id in sorted(str(m) for m in wanted):
            snapshot = await self._db.document(MODELS, model_id).get()
            if not snapshot.exists:
                missing.append(model_id)
        if missing:
            raise InvalidStateError(
                f"stage model(s) not in the models collection: {', '.join(missing)}"
            )

    async def update(self, agent_ref: str, payload: AgentUpdate) -> dict[str, Any]:
        agent = await self.get(agent_ref)

        patch: dict[str, Any] = {}
        for field, value in payload.model_dump(exclude_unset=True).items():
            patch[field] = value.value if isinstance(value, AgentStatus) else value

        if not patch:
            return agent

        if "stages" in patch:
            await self._reject_unknown_stage_models(patch["stages"])

        return await self._apply_patch(agent, patch)

    async def set_status(self, agent_ref: str, status: AgentStatus) -> dict[str, Any]:
        agent = await self.get(agent_ref)
        return await self._apply_patch(agent, {"status": status.value})

    async def soft_delete(self, agent_ref: str) -> dict[str, Any]:
        """Mark an agent deleted without removing it.

        Runs and feedback keep referring to it, so the row must survive.
        """

        agent = await self.get(agent_ref)
        now = utcnow()
        return await self._apply_patch(
            agent, {"deleted_at": now, "status": AgentStatus.DISABLED.value}
        )

    async def restore(self, agent_ref: str) -> dict[str, Any]:
        agent = await self.get(agent_ref, include_deleted=True)
        if agent.get("deleted_at") is None:
            return agent
        return await self._apply_patch(agent, {"deleted_at": None})

    async def _apply_patch(self, agent: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        patch["updated_at"] = utcnow()
        await self._db.document(AGENTS, agent["id"]).update(patch)
        return {**agent, **patch}

    # --- Reads -------------------------------------------------------------

    async def get(self, agent_ref: str, *, include_deleted: bool = False) -> dict[str, Any]:
        """Fetch one agent by id (its slug). Raises if missing or deleted."""

        snapshot = await self._db.document(AGENTS, agent_ref).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("agent", agent_ref)

        agent = snapshot_to_dict(snapshot)
        if agent.get("deleted_at") is not None and not include_deleted:
            raise ResourceNotFoundError("agent", agent_ref)
        return agent

    async def get_active(self, agent_ref: str) -> dict[str, Any]:
        """Fetch an agent that is allowed to run, or raise a conflict."""

        agent = await self.get(agent_ref)
        if agent.get("status") != AgentStatus.ACTIVE.value:
            raise ResourceConflictError(
                f"agent '{agent_ref}' is {agent.get('status')} and cannot be dispatched"
            )
        return agent

    async def list_agents(
        self,
        *,
        status: AgentStatus | None = None,
        agent_type: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List agents, newest first.

        The agent collection is admin-sized (tens to hundreds of documents), so
        it is streamed once and filtered in process. That keeps free-text search
        and multi-field filtering available without demanding a composite index
        for every combination the portal might ask for.
        """

        agents = [
            snapshot_to_dict(snapshot)
            async for snapshot in self._db.collection(AGENTS).stream()
        ]

        needle = query.lower().strip() if query else None
        matches = [
            agent
            for agent in agents
            if (include_deleted or agent.get("deleted_at") is None)
            and (status is None or agent.get("status") == status.value)
            and (agent_type is None or agent.get("agent_type") == agent_type)
            and (tag is None or tag in (agent.get("tags") or []))
            and (
                needle is None
                or needle in (agent.get("slug") or "").lower()
                or needle in (agent.get("name") or "").lower()
                or needle in (agent.get("description") or "").lower()
            )
        ]
        matches.sort(key=lambda agent: agent.get("created_at") or utcnow(), reverse=True)

        return matches[offset : offset + limit], len(matches)
