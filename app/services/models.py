"""The model catalog and its access requests.

Same conventions as agents and templates: a model's document id is its
``model_id``, so "one document per model" is enforced by the path rather than
by an application check, and an agent stage storing a ``modelId`` is storing a
reference that either resolves or does not.
"""

from __future__ import annotations

import logging
from typing import Any

from google.api_core.exceptions import AlreadyExists

from app.api.schemas.model import (
    ModelAccessRequest,
    ModelAliasUpsert,
    ModelCreate,
    ModelUpdate,
    PricingCoverage,
    UnpricedModelReference,
    alias_document,
    model_document,
)
from app.core.enums import ModelAvailability
from app.core.exceptions import ResourceConflictError, ResourceNotFoundError
from app.db.firestore import (
    AGENTS,
    MODEL_ACCESS_REQUESTS,
    MODEL_ALIASES,
    MODELS,
    FirestoreDB,
    generate_id,
    snapshot_to_dict,
    utcnow,
)

logger = logging.getLogger(__name__)

#: `ModelService.list` shadows the builtin inside the class body, so an
#: annotation of `list[...]` on a method resolves to the method. Naming the
#: shape once avoids writing `builtins.list` at every call site.
Rows = list[dict[str, Any]]


class ModelService:
    """Reads and writes the normalized model catalog."""

    def __init__(self, db: FirestoreDB) -> None:
        self._db = db

    async def create(self, payload: ModelCreate) -> dict[str, Any]:
        document = model_document(payload, utcnow())
        try:
            await self._db.document(MODELS, payload.model_id).create(document)
        except AlreadyExists as exc:
            raise ResourceConflictError(f"model '{payload.model_id}' already exists") from exc
        return document

    async def get(self, model_id: str) -> dict[str, Any]:
        snapshot = await self._db.document(MODELS, model_id).get()
        # `snapshot.exists`, not a None check on the flattened dict:
        # `snapshot_to_dict` always returns at least `{"id": ...}`, so a None
        # check silently passes for a document that is not there.
        if not snapshot.exists:
            raise ResourceNotFoundError("model", model_id)
        return snapshot_to_dict(snapshot)

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        availability: ModelAvailability | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Every model, newest-irrelevant: this is a small catalog read whole.

        Filtered in process rather than by query because the collection is
        admin-sized and a Firestore composite index for one optional filter is
        not worth carrying. Returns the total so a caller can page honestly.
        """
        rows: list[dict[str, Any]] = []
        async for snapshot in self._db.collection(MODELS).stream():
            row = snapshot_to_dict(snapshot)
            if availability is not None and row.get("availability") != availability.value:
                continue
            rows.append(row)

        # Stable order the UI can rely on: selectable first, then by name. A
        # dropdown that reorders itself between loads is its own bug.
        order = {
            ModelAvailability.AVAILABLE.value: 0,
            ModelAvailability.NOT_ENABLED.value: 1,
            ModelAvailability.RETIRED.value: 2,
        }
        rows.sort(
            key=lambda r: (
                order.get(str(r.get("availability")), 9),
                str(r.get("display_name", "")),
            )
        )
        return rows[offset : offset + limit], len(rows)

    async def update(self, model_id: str, payload: ModelUpdate) -> dict[str, Any]:
        patch: dict[str, Any] = {
            key: (value.value if hasattr(value, "value") else value)
            for key, value in payload.model_dump(exclude_unset=True).items()
        }
        if not patch:
            return await self.get(model_id)
        await self.get(model_id)  # 404 before write
        patch["updated_at"] = utcnow()
        await self._db.document(MODELS, model_id).update(patch)
        return await self.get(model_id)

    async def request_access(self, model_id: str, payload: ModelAccessRequest) -> dict[str, Any]:
        """Records that someone wants a model this deployment does not route.

        Recorded, never actioned: enabling a model means the engine has to
        route it and someone has to accept its cost, so this captures the ask
        and a human does the enabling. Allowed against any model -- including
        one already available, which is how a duplicate request surfaces as
        data rather than as an error the requester has to interpret.
        """
        model = await self.get(model_id)
        document = {
            "id": generate_id(),
            "model_id": model["model_id"],
            "requested_by": payload.requested_by,
            "reason": payload.reason,
            "agent_id": payload.agent_id,
            "status": "open",
            "created_at": utcnow(),
        }
        await self._db.document(MODEL_ACCESS_REQUESTS, document["id"]).create(document)
        logger.info(
            "model access requested",
            extra={"model_id": model_id, "requested_by": payload.requested_by},
        )
        return document

    # --- Pricing ------------------------------------------------------------

    async def pricing_for(self, model_id: str) -> dict[str, Any]:
        """The price of one model, or a 404 that names it.

        This is the method the whole of S12 exists for. `pricingForModel` in
        the engine, and `computeCostUsd` in the portal, both answer a miss with
        Sonnet's $3/$15 and no signal -- which bills Opus work at a third of
        its real cost and the Gemini fallback at many times its own. A
        plausible wrong number is the worst failure available in a cost report,
        because nothing about it looks broken.

        So: no default. A model the catalog cannot price raises.
        """

        row = await self.get(model_id)
        if row.get("input_per_1m") is None or row.get("output_per_1m") is None:
            raise ResourceNotFoundError("pricing for model", model_id)
        return {
            "model_id": row["model_id"],
            "input_per_1m": row["input_per_1m"],
            "output_per_1m": row["output_per_1m"],
            "cached_input_per_1m": row.get("cached_input_per_1m"),
            "pricing_source": row.get("pricing_source"),
            "pricing_checked_on": row.get("pricing_checked_on"),
        }

    async def priced_model_ids(self) -> set[str]:
        """Every model id the catalog can price. One read, for the guard."""

        priced: set[str] = set()
        async for snapshot in self._db.collection(MODELS).stream():
            row = snapshot_to_dict(snapshot)
            if row.get("input_per_1m") is not None and row.get("output_per_1m") is not None:
                priced.add(str(row.get("model_id") or row.get("id")))
        return priced

    async def coverage(self) -> PricingCoverage:
        """Which models the agents name, and which of those cannot be priced.

        The pre-flight for turning enforcement on. Without it, flipping
        ``require_priced_models`` in an environment whose catalog is not seeded
        turns every dispatch into a 422, and the way you find out is a client
        noticing nothing ran.

        Reads agents rather than trusting a list, because the models that
        matter are the ones actually referenced -- an unpriced row nobody names
        is a tidiness problem, and a named model with no row is an outage.
        """

        known: dict[str, dict[str, Any]] = {}
        async for snapshot in self._db.collection(MODELS).stream():
            row = snapshot_to_dict(snapshot)
            known[str(row.get("model_id") or row.get("id"))] = row

        referenced: set[str] = set()
        gaps: list[UnpricedModelReference] = []

        async for snapshot in self._db.collection(AGENTS).stream():
            agent = snapshot_to_dict(snapshot)
            agent_id = str(agent.get("id", ""))
            agent_slug = str(agent.get("slug", ""))

            for model_id, stage_id in _model_references(agent):
                referenced.add(model_id)
                catalog_row = known.get(model_id)
                if catalog_row is None:
                    reason = "missing"
                elif (
                    catalog_row.get("input_per_1m") is None
                    or catalog_row.get("output_per_1m") is None
                ):
                    reason = "unpriced"
                else:
                    continue
                gaps.append(
                    UnpricedModelReference(
                        model_id=model_id,
                        agent_id=agent_id,
                        agent_slug=agent_slug,
                        stage_id=stage_id,
                        reason=reason,
                    )
                )

        priced = sorted(
            model_id
            for model_id, row in known.items()
            if row.get("input_per_1m") is not None and row.get("output_per_1m") is not None
        )
        return PricingCoverage(
            referenced_models=sorted(referenced), priced_models=priced, gaps=gaps
        )

    # --- Aliases ------------------------------------------------------------

    async def upsert_alias(self, alias: str, payload: ModelAliasUpsert) -> dict[str, Any]:
        """Point an alias at a model. Idempotent, because repointing IS the job.

        An alias exists so that a new model generation is a data change rather
        than a code change and a redeploy. If pointing it somewhere new
        required deleting and recreating it, the operation everyone actually
        performs would be the one with a window where the alias does not
        resolve.
        """

        model = await self.get(payload.model_id)
        if model.get("input_per_1m") is None:
            raise ResourceConflictError(
                f"model '{payload.model_id}' has no price, so alias '{alias}' would "
                "resolve to a model nothing can cost"
            )

        now = utcnow()
        document = alias_document(alias, payload, now)
        existing = await self._db.document(MODEL_ALIASES, alias).get()
        if existing.exists:
            document["created_at"] = snapshot_to_dict(existing).get("created_at", now)
            await self._db.document(MODEL_ALIASES, alias).update(
                {
                    "model_id": document["model_id"],
                    "provider_policy": document["provider_policy"],
                    "description": document["description"],
                    "updated_at": now,
                }
            )
        else:
            await self._db.document(MODEL_ALIASES, alias).create(document)

        logger.info(
            "model alias set",
            extra={"alias": alias, "model_id": payload.model_id},
        )
        return await self.get_alias(alias)

    async def get_alias(self, alias: str) -> dict[str, Any]:
        """Resolve an alias, denormalizing what the caller needs next."""

        snapshot = await self._db.document(MODEL_ALIASES, alias).get()
        if not snapshot.exists:
            raise ResourceNotFoundError("model alias", alias)
        row = snapshot_to_dict(snapshot)
        try:
            model = await self.get(str(row["model_id"]))
        except ResourceNotFoundError:
            # An alias pointing at a model that has since been removed. Not
            # silently repaired: the alias is what a stage stores, so this is a
            # broken reference someone has to fix, and hiding it would make a
            # dispatch fail somewhere further away from the cause.
            row["provider_model_name"] = None
            row["region"] = None
            return row
        row["provider_model_name"] = model.get("provider_model_name")
        row["region"] = model.get("region")
        return row

    async def list_aliases(self) -> Rows:
        aliases: Rows = []
        async for snapshot in self._db.collection(MODEL_ALIASES).stream():
            aliases.append(await self.get_alias(str(snapshot_to_dict(snapshot)["alias"])))
        aliases.sort(key=lambda row: str(row.get("alias", "")))
        return aliases

    async def resolve(self, model_or_alias: str) -> dict[str, Any]:
        """A model row, whether the caller named the model or an alias for it.

        Model first: a model id and an alias could in principle collide, and
        resolving the concrete thing first means adding an alias can never
        change what an existing stage means.
        """

        try:
            return await self.get(model_or_alias)
        except ResourceNotFoundError:
            alias = await self.get_alias(model_or_alias)
            return await self.get(str(alias["model_id"]))


def _model_references(agent: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Every (model_id, stage_id) an agent names. Stage id is None for its own default."""

    references: list[tuple[str, str | None]] = []
    default_model = agent.get("model")
    if isinstance(default_model, str) and default_model:
        references.append((default_model, None))

    stages = agent.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            model_id = stage.get("model_id") or stage.get("modelId")
            if isinstance(model_id, str) and model_id:
                references.append((model_id, str(stage.get("id") or "")))
    return references
