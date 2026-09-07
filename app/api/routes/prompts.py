"""System prompt versions and few-shot examples of an agent.

Prompt versions are append-only: there is no PUT and no DELETE. Editing a prompt
means posting a new version, and the portal chooses which version is live with
``/activate`` -- so a run can always be traced back to the exact text it used.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.schemas.common import OperationResult, Page, Pagination, pagination, parse_rows
from app.api.schemas.prompt import (
    FewShotExampleCreate,
    FewShotExampleRead,
    FewShotExampleUpdate,
    SystemPromptCreate,
    SystemPromptRead,
)
from app.core.enums import ExampleSource
from app.core.roles import Role
from app.dependencies import get_prompt_service, resolve_agent
from app.security import CallerIdentity, require_role
from app.services.prompts import PromptService

router = APIRouter(prefix="/agents/{agent_id}", tags=["prompts"])


# --- System prompts --------------------------------------------------------


@router.post(
    "/prompts",
    response_model=SystemPromptRead,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new system prompt version",
)
async def create_prompt_version(
    payload: SystemPromptCreate,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> SystemPromptRead:
    prompt = await prompts.create_version(agent["id"], payload, identity.actor)
    return SystemPromptRead.model_validate(prompt)


@router.get(
    "/prompts",
    response_model=list[SystemPromptRead],
    summary="List every system prompt version, newest first",
)
async def list_prompt_versions(
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> list[SystemPromptRead]:
    versions = await prompts.list_versions(agent["id"])
    return [SystemPromptRead.model_validate(version) for version in versions]


@router.get(
    "/prompts/active",
    response_model=SystemPromptRead,
    summary="Get the active system prompt",
)
async def get_active_prompt(
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> SystemPromptRead:
    prompt = await prompts.get_active(agent["id"])
    return SystemPromptRead.model_validate(prompt)


@router.get(
    "/prompts/{version}",
    response_model=SystemPromptRead,
    summary="Get one system prompt version",
)
async def get_prompt_version(
    version: int,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> SystemPromptRead:
    prompt = await prompts.get_version(agent["id"], version)
    return SystemPromptRead.model_validate(prompt)


@router.post(
    "/prompts/{version}/activate",
    response_model=SystemPromptRead,
    summary="Make a version the active one",
    description="Also used to roll back: activate an older version.",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def activate_prompt_version(
    version: int,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> SystemPromptRead:
    prompt = await prompts.activate(agent["id"], version)
    return SystemPromptRead.model_validate(prompt)


# --- Few-shot examples -----------------------------------------------------


@router.post(
    "/examples",
    response_model=FewShotExampleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a few-shot example",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def create_example(
    payload: FewShotExampleCreate,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> FewShotExampleRead:
    example = await prompts.create_example(agent["id"], payload)
    return FewShotExampleRead.model_validate(example)


@router.get(
    "/examples",
    response_model=Page[FewShotExampleRead],
    summary="List few-shot examples in prompt order",
)
async def list_examples(
    page: Pagination = Depends(pagination),
    active_only: Annotated[bool, Query()] = False,
    tag: Annotated[str | None, Query()] = None,
    source: Annotated[ExampleSource | None, Query()] = None,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> Page[FewShotExampleRead]:
    items, total = await prompts.list_examples(
        agent["id"],
        active_only=active_only,
        tag=tag,
        source=source,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[FewShotExampleRead](
        items=parse_rows(FewShotExampleRead, items, collection="examples"),
        limit=page.limit,
        offset=page.offset,
        total=total,
        has_more=page.offset + len(items) < total,
    )


@router.get(
    "/examples/{example_id}",
    response_model=FewShotExampleRead,
    summary="Get one few-shot example",
)
async def get_example(
    example_id: str,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> FewShotExampleRead:
    example = await prompts.get_example(agent["id"], example_id)
    return FewShotExampleRead.model_validate(example)


@router.patch(
    "/examples/{example_id}",
    response_model=FewShotExampleRead,
    summary="Edit a few-shot example",
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def update_example(
    example_id: str,
    payload: FewShotExampleUpdate,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> FewShotExampleRead:
    example = await prompts.update_example(agent["id"], example_id, payload)
    return FewShotExampleRead.model_validate(example)


@router.delete(
    "/examples/{example_id}",
    response_model=OperationResult,
    summary="Delete a few-shot example",
    description=(
        "Examples are disposable teaching material, so this really removes the "
        "document. Set ``is_active=false`` instead to keep it for reference."
    ),
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def delete_example(
    example_id: str,
    response: Response,
    agent: dict[str, Any] = Depends(resolve_agent),
    prompts: PromptService = Depends(get_prompt_service),
) -> OperationResult:
    await prompts.delete_example(agent["id"], example_id)
    response.status_code = status.HTTP_200_OK
    return OperationResult(detail=f"example '{example_id}' deleted")
