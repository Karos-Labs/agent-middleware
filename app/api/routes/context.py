"""Context resolution and job dispatch.

``GET /agents/{agent_id}/context`` is the call the portal makes when launching a
task: one request returns the agent's settings, its active system prompt, its
few-shot examples and the active version of the relevant template, ready to be
injected into a job payload.

``POST /agents/{agent_id}/jobs`` is the same resolution plus the publish: the
middleware records the run and puts the payload on the engine topic itself, so
the run, the payload and the message can never disagree.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.schemas.context import AgentContext, DispatchRequest, DispatchResponse, JobPayload
from app.api.schemas.run import RunRead
from app.core.roles import Role
from app.dependencies import get_context_service, get_dispatch_service, resolve_agent
from app.security import require_role
from app.services.context import ContextService
from app.services.dispatch import DispatchService
from app.services.templates import DEFAULT_PURPOSE

router = APIRouter(prefix="/agents/{agent_id}", tags=["context & dispatch"])


@router.get(
    "/context",
    response_model=AgentContext,
    summary="Resolve the full context of an agent",
    description=(
        "Everything dynamic about the agent, resolved now: active system prompt, "
        "active few-shot examples and the active version of the template bound to "
        "``purpose``. This is what the portal injects when creating a job."
    ),
)
async def get_agent_context(
    purpose: Annotated[str, Query(description="Template purpose to resolve")] = DEFAULT_PURPOSE,
    template: Annotated[
        str | None, Query(description="Template id, overriding the agent binding")
    ] = None,
    include_examples: Annotated[bool, Query()] = True,
    max_examples: Annotated[int | None, Query(ge=0, le=200)] = None,
    require_active: Annotated[
        bool, Query(description="Refuse to resolve a disabled agent")
    ] = False,
    agent: dict[str, Any] = Depends(resolve_agent),
    context: ContextService = Depends(get_context_service),
) -> AgentContext:
    return await context.build(
        agent["id"],
        purpose=purpose,
        template_ref=template,
        include_examples=include_examples,
        max_examples=max_examples,
        require_active=require_active,
    )


@router.post(
    "/payload",
    response_model=JobPayload,
    summary="Preview the job payload without publishing it",
    description=(
        "Builds exactly what ``/jobs`` would publish, but neither records a run "
        "nor sends anything. Useful for debugging a payload from the portal."
    ),
)
async def preview_job_payload(
    request: DispatchRequest,
    agent: dict[str, Any] = Depends(resolve_agent),
    dispatch: DispatchService = Depends(get_dispatch_service),
) -> JobPayload:
    return await dispatch.build_preview(agent["id"], request)


@router.post(
    "/jobs",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch a job to the engine",
    description=(
        "Resolves the context, records a run, and publishes the payload to the "
        "job topic. The engine receives everything it needs in the message and "
        "never reads this database."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def dispatch_job(
    request: DispatchRequest,
    agent: dict[str, Any] = Depends(resolve_agent),
    dispatch: DispatchService = Depends(get_dispatch_service),
) -> DispatchResponse:
    run, _payload, message_id = await dispatch.dispatch(agent["id"], request)
    return DispatchResponse(
        run=RunRead.model_validate(run),
        topic=dispatch.topic_path,
        pubsub_message_id=message_id,
    )
