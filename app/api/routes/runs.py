"""Runs and the feedback / evaluation store.

The portal registers a run (or dispatches one through ``/jobs``), the engine
reports the result back with PATCH, and reviewers attach ratings and corrections.
``/feedback/examples`` then reads that store back as few-shot candidates, and
``/promote`` turns a chosen one into a real example for the agent -- closing the
loop from evaluation to prompt quality.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.schemas.common import Page, Pagination, pagination, parse_rows
from app.api.schemas.prompt import FewShotExampleRead
from app.api.schemas.run import (
    FeedbackCreate,
    FeedbackExample,
    FeedbackPromoteRequest,
    FeedbackRead,
    RunCreate,
    RunRead,
    RunUpdate,
)
from app.core.enums import FeedbackStatus, RunStatus
from app.core.roles import Role
from app.dependencies import get_feedback_service, get_run_service, resolve_agent
from app.security import require_role
from app.services.feedback import FeedbackService
from app.services.runs import RunService

router = APIRouter(prefix="/agents/{agent_id}", tags=["runs & feedback"])

#: Tenant-scoped rather than agent-scoped, so a run can be attributed to a
#: client without knowing which agent produced it. Same module as the
#: agent-scoped routes because it is the same collection and the same
#: service; a second router only because the path prefix differs -- the
#: arrangement templates.py already uses.
client_router = APIRouter(prefix="/clients", tags=["runs & feedback"])


# --- Runs ------------------------------------------------------------------


@router.post(
    "/runs",
    response_model=RunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a run",
    description=(
        "For portals that publish the job payload themselves. Registering the run "
        "is what lets feedback be attached to the prompt and template version it "
        "used. Dispatching through ``POST /agents/{agent_id}/jobs`` does this "
        "automatically."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def register_run(
    payload: RunCreate,
    agent: dict[str, Any] = Depends(resolve_agent),
    runs: RunService = Depends(get_run_service),
) -> RunRead:
    run = await runs.register(agent["id"], payload)
    return RunRead.model_validate(run)


@router.get(
    "/runs",
    response_model=Page[RunRead],
    summary="List runs of an agent, newest first",
)
async def list_runs(
    page: Pagination = Depends(pagination),
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    client_slug: Annotated[
        str | None, Query(description="Only this tenant's runs of this agent")
    ] = None,
    agent: dict[str, Any] = Depends(resolve_agent),
    runs: RunService = Depends(get_run_service),
) -> Page[RunRead]:
    items, has_more = await runs.list_for_agent(
        agent["id"],
        status=run_status,
        client_slug=client_slug,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[RunRead](
        items=parse_rows(RunRead, items, collection="agent_runs"),
        limit=page.limit,
        offset=page.offset,
        has_more=has_more,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunRead,
    summary="Get a run with its feedback",
)
async def get_run(
    run_id: str,
    agent: dict[str, Any] = Depends(resolve_agent),
    runs: RunService = Depends(get_run_service),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> RunRead:
    run = await runs.get(run_id, agent_id=agent["id"])
    run["feedback"] = await feedback.list_for_run(agent["id"], run_id)
    return RunRead.model_validate(run)


@router.patch(
    "/runs/{run_id}",
    response_model=RunRead,
    summary="Report a run result",
    description=(
        "The engine (or the portal on its behalf) reports status, the produced "
        "artifact, or an error. A terminal status stamps ``completed_at``."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def update_run(
    run_id: str,
    payload: RunUpdate,
    agent: dict[str, Any] = Depends(resolve_agent),
    runs: RunService = Depends(get_run_service),
) -> RunRead:
    run = await runs.update(agent["id"], run_id, payload)
    return RunRead.model_validate(run)


# --- Feedback --------------------------------------------------------------


@router.post(
    "/runs/{run_id}/feedback",
    response_model=FeedbackRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit feedback for a run",
    description=(
        "Rating 1-5, an approved/rejected/needs_changes verdict, correction notes "
        "and optionally the corrected output. Several reviewers may each leave "
        "feedback on the same run."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def create_feedback(
    run_id: str,
    payload: FeedbackCreate,
    agent: dict[str, Any] = Depends(resolve_agent),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> FeedbackRead:
    stored = await feedback.add(agent["id"], run_id, payload)
    return FeedbackRead.model_validate(stored)


@router.get(
    "/runs/{run_id}/feedback",
    response_model=list[FeedbackRead],
    summary="List the feedback on one run",
)
async def list_run_feedback(
    run_id: str,
    agent: dict[str, Any] = Depends(resolve_agent),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> list[FeedbackRead]:
    items = await feedback.list_for_run(agent["id"], run_id)
    return [FeedbackRead.model_validate(item) for item in items]


@router.get(
    "/feedback",
    response_model=Page[FeedbackRead],
    summary="List feedback for an agent, best rated first",
)
async def list_agent_feedback(
    page: Pagination = Depends(pagination),
    min_rating: Annotated[int | None, Query(ge=1, le=5)] = None,
    feedback_status: Annotated[FeedbackStatus | None, Query(alias="status")] = None,
    agent: dict[str, Any] = Depends(resolve_agent),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> Page[FeedbackRead]:
    items, has_more = await feedback.list_for_agent(
        agent["id"],
        min_rating=min_rating,
        status=feedback_status,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[FeedbackRead](
        items=parse_rows(FeedbackRead, items, collection="run_feedback"),
        limit=page.limit,
        offset=page.offset,
        has_more=has_more,
    )


@router.get(
    "/feedback/examples",
    response_model=Page[FeedbackExample],
    summary="Read the feedback store back as few-shot candidates",
    description=(
        "Well-rated, approved runs rendered as input/output pairs -- the material "
        "for improving future runs. Defaults to rating >= 4 and status=approved. "
        "Promote the ones worth keeping."
    ),
)
async def list_feedback_examples(
    page: Pagination = Depends(pagination),
    min_rating: Annotated[int, Query(ge=1, le=5)] = 4,
    feedback_status: Annotated[FeedbackStatus | None, Query(alias="status")] = (
        FeedbackStatus.APPROVED
    ),
    agent: dict[str, Any] = Depends(resolve_agent),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> Page[FeedbackExample]:
    items, has_more = await feedback.candidate_examples(
        agent["id"],
        min_rating=min_rating,
        status=feedback_status,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[FeedbackExample](
        items=[FeedbackExample.model_validate(item) for item in items],
        limit=page.limit,
        offset=page.offset,
        has_more=has_more,
    )


@router.post(
    "/feedback/{feedback_id}/promote",
    response_model=FewShotExampleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Promote feedback into a few-shot example",
    description=(
        "Creates an active example from the reviewer's corrected output (or the "
        "run's own output) and links it back to the feedback it came from."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def promote_feedback(
    feedback_id: str,
    payload: FeedbackPromoteRequest,
    agent: dict[str, Any] = Depends(resolve_agent),
    feedback: FeedbackService = Depends(get_feedback_service),
) -> FewShotExampleRead:
    example = await feedback.promote(agent["id"], feedback_id, payload)
    return FewShotExampleRead.model_validate(example)


@client_router.get(
    "/{client_slug}/runs",
    response_model=Page[RunRead],
    summary="Every run for one tenant, across all agents",
    description=(
        "Runs dispatched before `client_slug` was stored are absent from this listing and "
        "cannot be recovered: nothing else on a run document names a tenant."
    ),
)
async def list_client_runs(
    client_slug: str,
    page: Pagination = Depends(pagination),
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    runs: RunService = Depends(get_run_service),
) -> Page[RunRead]:
    items, has_more = await runs.list_for_client(
        client_slug, status=run_status, limit=page.limit, offset=page.offset
    )
    return Page[RunRead](
        items=parse_rows(RunRead, items, collection="agent_runs"),
        limit=page.limit,
        offset=page.offset,
        has_more=has_more,
    )
