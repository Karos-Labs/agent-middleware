"""Builds a job payload from an agent's context and publishes it to the engine.

The portal may do this itself -- fetch the context, publish, then register the run
-- but doing it here in one call keeps the run record, the payload and the
Pub/Sub message consistent by construction, which is what makes feedback
attributable to an exact prompt and template version.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.api.schemas.context import (
    JOB_PAYLOAD_SCHEMA_VERSION,
    AgentContext,
    DispatchRequest,
    JobPayload,
)
from app.api.schemas.presentation import AgentStage
from app.config import Settings
from app.core.enums import RunStatus
from app.core.exceptions import MessagePublishError, ResourceNotFoundError
from app.db.firestore import utcnow
from app.services.context import ContextService
from app.services.models import ModelService
from app.services.publisher import PublisherService
from app.services.runs import RunService

logger = logging.getLogger(__name__)


class DispatchService:
    """Publishes agent jobs and records them as runs."""

    def __init__(
        self,
        settings: Settings,
        context: ContextService,
        runs: RunService,
        publisher: PublisherService,
        models: ModelService,
    ) -> None:
        self._settings = settings
        self._context = context
        self._runs = runs
        self._publisher = publisher
        self._models = models

    @property
    def topic_path(self) -> str:
        return self._publisher.topic_path_for(self._settings.job_topic_id)

    async def build_preview(self, agent_ref: str, request: DispatchRequest) -> JobPayload:
        """Build the payload a dispatch would publish, without any side effects."""

        context = await self._context.build_runnable(
            agent_ref,
            purpose=request.template_purpose,
            template_ref=request.template_ref,
            include_examples=request.include_examples,
            max_examples=request.max_examples,
        )
        return _build_payload(context, request, request.run_id or "preview")

    async def dispatch(
        self, agent_ref: str, request: DispatchRequest
    ) -> tuple[dict[str, Any], JobPayload, str]:
        """Resolve, record and publish a job.

        Returns the run document, the published payload and the Pub/Sub message
        id. The run is written *before* publishing so a message can never exist
        without a run to attach feedback to; if publishing then fails the run is
        marked failed and the error propagates.
        """

        context = await self._context.build_runnable(
            agent_ref,
            purpose=request.template_purpose,
            template_ref=request.template_ref,
            include_examples=request.include_examples,
            max_examples=request.max_examples,
        )

        run_id = request.run_id or str(uuid.uuid4())
        payload = _build_payload(context, request, run_id)
        stage_models = await self._stage_models_for_engine(context.agent.stages)

        run = await self._runs.create(
            context.agent.id,
            run_id=run_id,
            status=RunStatus.PENDING,
            job_type=request.job_type,
            prompt_id=context.system_prompt.id if context.system_prompt else None,
            prompt_version=context.system_prompt.version if context.system_prompt else None,
            template_version_id=context.template.version_id if context.template else None,
            # Deliberately a reference snapshot, not the whole payload: prompt and
            # template versions are immutable, so the job can be reconstructed
            # from them without copying bodies into every run document.
            input_payload=_run_snapshot(context, request),
            requested_by=request.requested_by,
        )

        try:
            message_id = await self._publisher.publish_async(
                data=json.dumps(
                    to_engine_message(payload, stage_models), ensure_ascii=False
                ).encode("utf-8"),
                attributes=_message_attributes(context, request, run_id),
                topic_id=self._settings.job_topic_id,
            )
        except MessagePublishError as exc:
            await self._runs.update_failure(run_id, str(exc))
            raise

        await self._runs.mark_dispatched(run_id, message_id)
        logger.info(
            "Dispatched run %s for agent %s to %s (message=%s)",
            run_id,
            context.agent.slug,
            self.topic_path,
            message_id,
        )

        run.update(
            {
                "status": RunStatus.DISPATCHED.value,
                "pubsub_message_id": message_id,
            }
        )
        return run, payload, message_id

    async def _stage_models_for_engine(self, stages: list[AgentStage]) -> dict[str, str]:
        """The ``stageModels`` map exactly as ``applyStageModelOverride`` reads it.

        Two translations, both of which the first version of this feature
        skipped, which is why a Studio pick was stored, displayed and ignored:

        * the KEY is the engine's agent id (``x-draft``), not the workflow step
          id (``10-draft-post``) -- the engine looks the override up by
          ``this.config.id``. A stage seeded before ``agent_id`` existed falls
          back to its step id, which at least fails the same way it always did.
        * the VALUE is the model's ``provider_model_name`` (``claude-opus-4-8``),
          the id the engine's own catalog knows, not the catalog document id
          (``claude-opus-4-8-on-vertex``), which the engine would refuse.
        """
        out: dict[str, str] = {}
        for stage in stages:
            if not stage.model_id:
                continue
            try:
                model = await self._models.get(stage.model_id)
                provider_name = str(model.get("provider_model_name") or stage.model_id)
            except ResourceNotFoundError:
                # Validated on the edit that set it; a model retired since is
                # still sent as-is so the engine's own refusal names it.
                provider_name = stage.model_id
            out[stage.agent_id or stage.id] = provider_name
        return out


def _build_payload(context: AgentContext, request: DispatchRequest, run_id: str) -> JobPayload:
    return JobPayload(
        schema_version=JOB_PAYLOAD_SCHEMA_VERSION,
        run_id=run_id,
        job_type=request.job_type,
        input=request.input,
        requested_by=request.requested_by,
        dispatched_at=utcnow(),
        agent=context.agent,
        system_prompt=context.system_prompt,
        few_shot_examples=context.few_shot_examples,
        template=context.template,
        resolved_at=context.resolved_at,
        client_slug=request.client_slug,
        # The agent's slug IS the engine's product id: `instagram-agent`,
        # `landing-builder-agent` and friends are named identically on both
        # sides, which is what lets one identifier route the whole way through.
        product_id=context.agent.slug,
        run_kind=request.run_kind,
    )


def to_engine_message(
    payload: JobPayload, stage_models: dict[str, str] | None = None
) -> dict[str, Any]:
    """The bytes actually published. Carries two contracts at once, on purpose.

    agent-engine's queue consumer validates the body against its own
    ``RunJobRequestSchema``, which requires exactly three camelCase keys at the
    top level -- ``clientSlug``, ``productId``, ``runKind`` -- and rejects
    anything without them. A message shaped only like this module's
    :class:`JobPayload` fails that check, gets nacked, and lands in the
    dead-letter topic after five attempts.

    Zod strips unknown keys rather than rejecting them, so the engine reads its
    three and ignores the rest. That lets one message satisfy today's engine
    *and* carry the resolved prompt, template and examples the engine will read
    once it knows how -- no second topic, no versioned cutover.

    The duplication (``client_slug`` and ``clientSlug`` both present) is the
    price of that, and it is deliberate: dropping the snake_case originals
    would make the payload inconsistent with every other schema here, and
    dropping the camelCase aliases would break the engine.
    """

    body = payload.model_dump(mode="json")
    body["clientSlug"] = payload.client_slug
    body["productId"] = payload.product_id
    body["runKind"] = payload.run_kind
    # Per-stage model selection, as the {engineAgentId: providerModelName} map
    # the engine reads (see `DispatchService._stage_models_for_engine`). Only
    # stages that actually name a model appear: an empty or absent map means
    # "every stage keeps its compiled default", which is what the engine
    # already does when the key is missing entirely.
    if stage_models:
        body["stageModels"] = dict(stage_models)
    return body


def _run_snapshot(context: AgentContext, request: DispatchRequest) -> dict[str, Any]:
    """What the run document keeps about the job it carried."""

    snapshot: dict[str, Any] = {
        "input": request.input,
        "template_purpose": request.template_purpose,
        "example_count": len(context.few_shot_examples),
    }
    if context.template is not None:
        snapshot["template_id"] = context.template.id
        snapshot["template_version"] = context.template.version
    return snapshot


def _message_attributes(
    context: AgentContext, request: DispatchRequest, run_id: str
) -> dict[str, str]:
    """Attributes that let the engine route and trace a message without parsing it."""

    attributes = {
        "source": "agent-middleware",
        "schema_version": str(JOB_PAYLOAD_SCHEMA_VERSION),
        "run_id": run_id,
        "agent_id": context.agent.id,
        "agent_slug": context.agent.slug,
        "clientSlug": request.client_slug,
        "productId": context.agent.slug,
    }
    if request.job_type:
        attributes["job_type"] = request.job_type
    if context.agent.agent_type:
        attributes["agent_type"] = context.agent.agent_type
    if context.system_prompt is not None:
        attributes["prompt_version"] = str(context.system_prompt.version)
    if context.template is not None:
        attributes["template_id"] = context.template.id
        attributes["template_version"] = str(context.template.version)

    # Caller-supplied attributes never override the ones above.
    for key, value in request.attributes.items():
        attributes.setdefault(key, value)
    return attributes
