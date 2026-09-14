"""Schedules: definitions per client, and the claim/settle protocol (S10).

Two audiences, two prefixes:

* ``/clients/{client_slug}/schedules`` -- staff and the portal's settings
  surfaces: create, list, pause and resume. Editor role.
* ``/schedules`` -- the executor: ``claim`` and ``settle``. Editor role, the
  same gate ``POST /agents/{id}/jobs`` stands behind, because a caller that
  can claim a fire can spend a client's credits.

Every claimed row carries ``bill_client_credits``. That field is the whole
point of the merge: ``/api/scheduler`` never asked it and passed
``charge: null`` for every fire. Here the executor cannot receive a fire
without receiving the answer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.schemas.common import Page, Pagination, SlugStr, pagination
from app.api.schemas.schedule import (
    ClaimedFire,
    ClaimRequest,
    ClaimResponse,
    ScheduleCreate,
    ScheduleRead,
    ScheduleStatus,
    SettleRequest,
    StatusChange,
)
from app.core.exceptions import ServiceUnavailableError
from app.core.roles import Role
from app.security import CallerIdentity, require_role
from app.services.schedules import ScheduleService

client_router = APIRouter(prefix="/clients/{client_slug}/schedules", tags=["schedules"])
router = APIRouter(prefix="/schedules", tags=["schedules"])


def get_schedule_service(request: Request) -> ScheduleService:
    """The service, or a 503 that says what is missing (S1)."""

    service: ScheduleService | None = getattr(request.app.state, "schedule_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "schedules live in the configuration database, which is not configured in "
            "this environment (CONFIG_DB_DSN is unset). The portal's own crons are "
            "unaffected; nothing here is on a run path yet."
        )
    return service


# --- Definitions --------------------------------------------------------------


@client_router.post(
    "",
    response_model=ScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a schedule for a client",
    description=(
        "`bill_client_credits` is required and has no default. A schedule that does not "
        "say who pays is not created."
    ),
)
async def create_schedule(
    client_slug: SlugStr,
    payload: ScheduleCreate,
    caller: CallerIdentity = Depends(require_role(Role.EDITOR)),
    service: ScheduleService = Depends(get_schedule_service),
) -> ScheduleRead:
    created = await service.create(
        client_slug=client_slug, created_by=caller.actor, **payload.model_dump()
    )
    return ScheduleRead.model_validate(created)


@client_router.get(
    "", response_model=Page[ScheduleRead], summary="A client's schedules, soonest first"
)
async def list_schedules(
    client_slug: SlugStr,
    page: Pagination = Depends(pagination),
    schedule_status: Annotated[ScheduleStatus | None, Query(alias="status")] = None,
    service: ScheduleService = Depends(get_schedule_service),
) -> Page[ScheduleRead]:
    items, has_more = await service.list_for_client(
        client_slug, status=schedule_status, limit=page.limit, offset=page.offset
    )
    return Page[ScheduleRead](
        items=[ScheduleRead.model_validate(i) for i in items],
        limit=page.limit,
        offset=page.offset,
        has_more=has_more,
    )


# Declared before ``/{schedule_id}`` so the literal path wins the match.
@router.get(
    "/in-flight",
    response_model=list[ScheduleRead],
    summary="Fires claimed and not yet settled",
    description="With `older_than_minutes`, only the ones that look vanished.",
)
async def list_in_flight(
    older_than_minutes: Annotated[int | None, Query(ge=0)] = None,
    service: ScheduleService = Depends(get_schedule_service),
) -> list[dict[str, Any]]:
    older = timedelta(minutes=older_than_minutes) if older_than_minutes is not None else None
    return await service.in_flight(older_than=older)


@router.get("/{schedule_id}", response_model=ScheduleRead, summary="One schedule")
async def get_schedule(
    schedule_id: str, service: ScheduleService = Depends(get_schedule_service)
) -> ScheduleRead:
    return ScheduleRead.model_validate(await service.get(schedule_id))


@router.post(
    "/{schedule_id}/status",
    response_model=ScheduleRead,
    summary="Pause, resume or complete a schedule",
    description=(
        "Resuming recomputes the next fire from now; a month of missed slots does not replay."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def change_status(
    schedule_id: str,
    payload: StatusChange,
    service: ScheduleService = Depends(get_schedule_service),
) -> ScheduleRead:
    return ScheduleRead.model_validate(await service.set_status(schedule_id, payload.status))


# --- The fire protocol --------------------------------------------------------


@router.post(
    "/claim",
    response_model=ClaimResponse,
    summary="Claim every due fire, with its billing decision",
    description=(
        "`SELECT ... FOR UPDATE SKIP LOCKED`: concurrent ticks get disjoint sets. Each fire "
        "carries `bill_client_credits`; settle each one with the returned `claim_id`."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def claim_fires(
    payload: ClaimRequest | None = None,
    service: ScheduleService = Depends(get_schedule_service),
) -> ClaimResponse:
    claim_id, fires = await service.claim_due(limit=(payload or ClaimRequest()).limit)
    return ClaimResponse(claim_id=claim_id, fires=[ClaimedFire.model_validate(f) for f in fires])


@router.post(
    "/{schedule_id}/settle",
    response_model=ScheduleRead,
    summary="Report how a claimed fire ended",
    description=(
        "Only the claim that took the row may settle it; a stale claim id is refused with 409."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def settle_fire(
    schedule_id: str,
    payload: SettleRequest,
    service: ScheduleService = Depends(get_schedule_service),
) -> ScheduleRead:
    settled = await service.settle(
        schedule_id,
        claim_id=payload.claim_id,
        job_id=payload.job_id,
        error=payload.error,
        disable=payload.disable,
    )
    return ScheduleRead.model_validate(settled)
