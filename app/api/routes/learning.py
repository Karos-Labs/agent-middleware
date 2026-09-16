"""The learning loop's API (C7 / SCRUM-461, 462, 463).

Four kinds of caller, one router:

* the ENGINE-facing direction is not here at all -- the engine reads files
  the projector wrote and never calls this service (C7 §4.3);
* the PORTAL's reconcile calls ``POST /runs/{run_id}/collect`` when a run
  completes, and ``POST /clients/{slug}/learning/feedback`` when a client
  acts on a draft;
* the PORTAL's readiness and review pages read ``GET /clients/{slug}/learning/
  {platform}`` -- the same seven payloads the next run will read, served from
  Postgres so the page needs no bucket access;
* a PERSON (or the setup run, through the collector) writes the settings, the
  preferences, the strategy map and the craft rules.

Every write here is EDITOR; the craft rules -- L1 is platform-wide and
reaches every client -- are ADMIN.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.api.schemas.common import SlugStr
from app.api.schemas.learning import (
    CollectRead,
    CraftRulesWrite,
    FeedbackEventCreate,
    FeedbackEventRead,
    FileOutcomeRead,
    LearningContextRead,
    LearningProjectionRead,
    Platform,
    PreferencesWrite,
    SettingsWrite,
    StrategyMapWrite,
    SubjectRowsRead,
)
from app.core.roles import Role
from app.dependencies import get_learning_service
from app.security import require_role
from app.services.learning import LearningService

router = APIRouter(prefix="/clients/{client_slug}/learning", tags=["learning loop"])
run_router = APIRouter(prefix="/runs", tags=["learning loop"])
admin_router = APIRouter(prefix="/learning", tags=["learning loop"])

_NO_BUCKET = (
    "Projection is unavailable: GCS_ARTIFACTS_BUCKET is not configured for this deployment. "
    "The learning tables are still readable and writable."
)


def _require_bucket(service: LearningService) -> None:
    if not service.can_project:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_NO_BUCKET)


# Literal paths first: `/preferences` and `/feedback` would otherwise be
# swallowed by `/{platform}` below (FastAPI matches in registration order).
# --- Feedback and preferences (B2) ------------------------------------------------


@router.post(
    "/feedback",
    response_model=FeedbackEventRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Record what the client did with a draft",
    description=(
        "Appends to the unified feedback log, moves the subject row of `runId` to the matching "
        "status, re-derives the preferences and re-projects the platform's files."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def record_feedback(
    client_slug: SlugStr,
    body: FeedbackEventCreate,
    service: LearningService = Depends(get_learning_service),
) -> FeedbackEventRead:
    result = await service.record_feedback(
        client_slug,
        platform=body.platform,
        action=body.action,
        run_id=body.run_id,
        account=body.account,
        reason=body.reason,
        original_text=body.original_text,
        final_text=body.final_text,
        actor=body.actor,
        at=body.at,
        source="import" if body.source_id else "portal",
        source_id=body.source_id,
        reproject=service.can_project,
    )
    return FeedbackEventRead.model_validate(result)


@router.get(
    "/preferences",
    response_model=dict[str, Any] | None,
    summary="The client-wide preferences (C7 §2.4)",
)
async def read_preferences(
    client_slug: SlugStr,
    service: LearningService = Depends(get_learning_service),
) -> dict[str, Any] | None:
    return await service.store.preferences(client_slug)


@router.put(
    "/preferences",
    response_model=dict[str, Any],
    summary="Set the human half of the preferences: never-topics and standing instructions",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def write_preferences(
    client_slug: SlugStr,
    body: PreferencesWrite,
    service: LearningService = Depends(get_learning_service),
) -> dict[str, Any]:
    prefs = await service.store.put_preferences(
        client_slug,
        never_topics=body.never_topics,
        standing_instructions=body.standing_instructions,
        updated_by=body.updated_by,
    )
    if service.can_project:
        # Preferences are client-wide; every platform's next run should see them.
        for platform in ("x", "linkedin", "reddit"):
            await service.project(client_slug, platform, projected_by="middleware-preferences")
    return prefs


# --- What the next run reads ------------------------------------------------


@router.get(
    "/{platform}",
    response_model=LearningContextRead,
    response_model_by_alias=True,
    summary="The learning context the next run on this platform will read",
    description=(
        "The seven C7 §2 payloads, keyed by file kind, built from the same tables the "
        "projector reads. `null` means the file would be absent."
    ),
)
async def read_learning_context(
    client_slug: SlugStr,
    platform: Platform,
    service: LearningService = Depends(get_learning_service),
) -> LearningContextRead:
    view = await service.context(client_slug, platform)
    return LearningContextRead.model_validate(view)


@router.get(
    "/{platform}/subjects",
    response_model=SubjectRowsRead,
    summary="The subject table (B1) for one platform, newest first",
)
async def list_subject_rows(
    client_slug: SlugStr,
    platform: Platform,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: LearningService = Depends(get_learning_service),
) -> SubjectRowsRead:
    rows = await service.store.subject_rows(client_slug, platform, limit=limit, offset=offset)
    return SubjectRowsRead(slug=client_slug, platform=platform, rows=rows)


# --- Projection, on demand ------------------------------------------------------


@router.post(
    "/{platform}/project",
    response_model=LearningProjectionRead,
    summary="Project the learning context into the engine workspace now",
    description=(
        "What every dispatch of a platform agent does on its own. Idempotent by content "
        "hash: an unchanged payload writes nothing and leaves `projectedAt` alone."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def project_learning_context(
    client_slug: SlugStr,
    platform: Platform,
    projected_by: Annotated[str, Query(max_length=64)] = "portal-request",
    service: LearningService = Depends(get_learning_service),
) -> LearningProjectionRead:
    _require_bucket(service)
    result = await service.project(client_slug, platform, projected_by=projected_by)
    return LearningProjectionRead(
        slug=result.slug,
        platform=result.platform,
        written=result.written,
        files=[FileOutcomeRead(**vars(f)) for f in result.files],
    )


# --- Collection, after a run ---------------------------------------------------


@run_router.post(
    "/{run_id}/collect",
    response_model=CollectRead,
    response_model_by_alias=True,
    summary="Collect a completed run's state files into the learning tables",
    description=(
        "Reads `state/runs/<runId>.json` and `state/<platform>/platform-state.json` from the "
        "workspace, upserts the subject row, the platform state and the record, re-derives "
        "the client's preferences and projects again. Idempotent on `run_id`; the portal's "
        "reconcile may call it on every pass. `collected: false` with a reason is an ordinary "
        "answer for a run that wrote nothing."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def collect_run(
    run_id: Annotated[str, Path(min_length=1, max_length=200)],
    collected_by: Annotated[str, Query(max_length=64)] = "portal-reconcile",
    service: LearningService = Depends(get_learning_service),
) -> CollectRead:
    _require_bucket(service)
    result = await service.collect(run_id, collected_by=collected_by)
    return CollectRead.model_validate(vars(result))


# --- Settings, strategy map, craft rules ------------------------------------------


@router.put(
    "/{platform}/settings",
    response_model=dict[str, Any],
    summary="Per-platform knobs: anti-repeat window, feedback rows shown, L2 sector",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def write_settings(
    client_slug: SlugStr,
    platform: Platform,
    body: SettingsWrite,
    service: LearningService = Depends(get_learning_service),
) -> dict[str, Any]:
    return await service.store.put_settings(
        client_slug,
        platform,
        anti_repeat_days=body.anti_repeat_days,
        feedback_rows=body.feedback_rows,
        sector=body.sector,
    )


@router.put(
    "/{platform}/strategy-map",
    response_model=dict[str, Any],
    summary="Upsert the strategy map (C1): problem × stage rows with a goal on every row",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def write_strategy_map(
    client_slug: SlugStr,
    platform: Platform,
    body: StrategyMapWrite,
    service: LearningService = Depends(get_learning_service),
) -> dict[str, Any]:
    saved = await service.store.put_strategy_map(
        client_slug,
        platform,
        source=body.source,
        built_at=body.built_at,
        audience=body.audience,
        default_mix=body.default_mix,
        rows=[row.model_dump() for row in body.rows],
        replace=body.replace,
    )
    if service.can_project:
        await service.project(client_slug, platform, projected_by="middleware-strategy-map")
    return saved


@admin_router.put(
    "/craft-rules",
    response_model=dict[str, int],
    summary="Bulk-upsert craft rules (D41): L1 platform base, L2 sector overlay, L3 client rules",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def write_craft_rules(
    body: CraftRulesWrite,
    service: LearningService = Depends(get_learning_service),
) -> dict[str, int]:
    rules = [rule.model_dump(by_alias=True) for rule in body.rules]
    count = await service.store.put_craft_rules(rules, updated_by=body.updated_by)
    return {"upserted": count}
