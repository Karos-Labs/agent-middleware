"""The prompt store agent-engine executes from.

Distinct from ``prompts.py``, which serves this service's own
``agents/{slug}/prompts`` subcollection. That one is append-only and versioned
because a run must be traceable to the exact text it used. This one is neither,
and the difference is not a lapse: a stage's ``skillRef`` pins a version in
compiled TypeScript, so the only write that changes what an agent runs is one
that replaces the content of the version already named. See
``app/services/engine_prompts.py`` for the full reasoning and for where the
superseded text goes.

Addressed by prompt id and version rather than by the ``id@version`` skillRef,
so no ``@`` ever has to survive a URL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from app.api.schemas.engine_prompt import (
    EnginePromptRead,
    EnginePromptUpdate,
    EnginePromptVersionSummary,
    PromptRestoreRequest,
    PromptVersionRead,
)
from app.core.roles import Role
from app.dependencies import get_engine_prompt_service, get_prompt_store
from app.security import CallerIdentity, require_role
from app.services.engine_prompts import EnginePromptService
from app.services.prompt_store import UnifiedPromptStore

router = APIRouter(prefix="/engine-prompts", tags=["engine prompts"])


@router.get(
    "/{prompt_id}/versions/{version}",
    response_model=EnginePromptRead,
    summary="Read the prompt text a stage will execute",
    description=(
        "Resolved exactly the way agent-engine's own FirestorePromptStore resolves it, "
        "so what this returns is what a run would load. Reading it any other way risks an "
        "editor that shows one prompt while the agent executes another."
    ),
)
async def read_version(
    prompt_id: str,
    version: str,
    prompts: EnginePromptService = Depends(get_engine_prompt_service),
) -> EnginePromptRead:
    return EnginePromptRead.model_validate(await prompts.read(f"{prompt_id}@{version}"))


@router.get(
    "/{prompt_id}",
    response_model=EnginePromptRead,
    summary="Read a prompt's latest version",
    description="Falls back to the prompt's own `latestVersion`, as an unpinned skillRef does.",
)
async def read_latest(
    prompt_id: str,
    prompts: EnginePromptService = Depends(get_engine_prompt_service),
) -> EnginePromptRead:
    return EnginePromptRead.model_validate(await prompts.read(prompt_id))


@router.put(
    "/{prompt_id}/versions/{version}",
    response_model=EnginePromptRead,
    status_code=status.HTTP_200_OK,
    summary="Replace the prompt text a stage executes",
    description=(
        "Updates this version in place, which is the only write that takes effect: a stage's "
        "skillRef names one version, so a new version would be inert until someone changed "
        "TypeScript. The superseded text is kept on the prompt document. Empty content is "
        "refused — a stage with no system prompt still runs, on nothing but its turn contract."
    ),
)
async def replace_version(
    prompt_id: str,
    version: str,
    payload: EnginePromptUpdate,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    prompts: EnginePromptService = Depends(get_engine_prompt_service),
) -> EnginePromptRead:
    # The actor is the verified caller, not a query parameter. It used to be
    # `?actor=`, free text compared against nothing -- so the audit trail for a
    # prompt edit recorded whatever the caller chose to send, including
    # nothing. A record that cannot be wrong is also not evidence.
    updated = await prompts.write(f"{prompt_id}@{version}", payload.content, identity.actor)
    return EnginePromptRead.model_validate(updated)


@router.get(
    "/{prompt_id}/history",
    response_model=list[EnginePromptVersionSummary],
    summary="Superseded revisions of a prompt, oldest first",
)
async def read_history(
    prompt_id: str,
    prompts: EnginePromptService = Depends(get_engine_prompt_service),
) -> list[Any]:
    return [
        EnginePromptVersionSummary.model_validate(row) for row in await prompts.history(prompt_id)
    ]

# --- The append-only store (S7 / SCRUM-221) ---------------------------------
#
# `/history` above reads the capped `supersededHistory` array. These read the
# store that replaced it: every version, individually fetchable, with a restore
# path. Both are served during the migration window because the array still
# holds entries for prompts nobody has edited since, and deleting the evidence
# that this migration happened is not an improvement.


@router.get(
    "/{prompt_id}/versions/{version}/store",
    response_model=list[PromptVersionRead],
    summary="Every version of this prompt, newest first",
    description=(
        "Uncapped. The array this replaces kept ten entries, so an agent whose prompt "
        "is tuned weekly lost its first quarter of history in a quarter — and the entry "
        "nobody could read back was always the one somebody wanted."
    ),
)
async def list_stored_versions(
    prompt_id: str,
    version: str,
    store: UnifiedPromptStore = Depends(get_prompt_store),
) -> list[PromptVersionRead]:
    return [
        PromptVersionRead.model_validate(row)
        for row in await store.versions(prompt_id, version)
    ]


@router.get(
    "/{prompt_id}/versions/{version}/store/{stored_version}",
    response_model=PromptVersionRead,
    summary="One exact version of this prompt",
    description=(
        "What the capped array could never give back: a single revision, by number, "
        "with its author, its hash and whether it is the one currently running."
    ),
)
async def read_stored_version(
    prompt_id: str,
    version: str,
    stored_version: int,
    store: UnifiedPromptStore = Depends(get_prompt_store),
) -> PromptVersionRead:
    return PromptVersionRead.model_validate(
        await store.version(prompt_id, version, stored_version)
    )


@router.post(
    "/{prompt_id}/versions/{version}/store/{stored_version}/restore",
    response_model=PromptVersionRead,
    summary="Reinstate an earlier version of this prompt",
    description=(
        "The restore path the old endpoint never had. Additive: the earlier text is "
        "written as a NEW version that records which one it reinstated, and the "
        "engine's document is updated to match. A restore that mutated history would "
        "be the same destructive edit wearing a different name."
    ),
)
async def restore_stored_version(
    prompt_id: str,
    version: str,
    stored_version: int,
    payload: PromptRestoreRequest,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    store: UnifiedPromptStore = Depends(get_prompt_store),
) -> PromptVersionRead:
    return PromptVersionRead.model_validate(
        await store.restore(
            prompt_id,
            version,
            stored_version,
            actor=identity.actor,
            reason=payload.reason,
        )
    )
