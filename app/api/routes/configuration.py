"""The Configuration API — authoring, publishing and rolling back agent versions.

Every write here names the role it needs and takes its actor from the verified
token rather than the body (S3): the audit log for a publish has to be evidence,
and a log recording whatever the caller typed is not.

The role floors are chosen by consequence, not by HTTP method:

* Reading a version or a diff -- ``viewer``.
* Authoring a draft -- ``editor``. A draft runs nothing.
* *Publishing and rolling back* -- ``admin``. Both change what every client of
  that agent gets on their next run, immediately, with no review step in
  between. That is a different kind of act from editing a draft, and the
  ladder should say so.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.schemas.configuration import (
    PublishRequest,
    PublishResult,
    RollbackRequest,
    StepsReplace,
    VersionCreate,
    VersionDiff,
    VersionRead,
    VersionSummary,
    VersionUpdate,
)
from app.core.exceptions import ServiceUnavailableError
from app.core.roles import Role
from app.security import CallerIdentity, require_role
from app.services.configuration import ConfigurationService

router = APIRouter(prefix="/config/agents", tags=["configuration"])


def get_configuration_service(request: Request) -> ConfigurationService:
    """The service, or a 503 that says what is missing.

    Not a 500: nothing is broken. ``CONFIG_DB_DSN`` is unset in this
    environment, which is a deployment state (S1 / SCRUM-216 is still open),
    and every other route is unaffected.
    """

    service: ConfigurationService | None = getattr(
        request.app.state, "configuration_service", None
    )
    if service is None:
        raise ServiceUnavailableError(
            "the configuration database is not configured in this environment "
            "(CONFIG_DB_DSN is unset), so agent versions cannot be read or written here"
        )
    return service


# --- Reading ----------------------------------------------------------------


@router.get(
    "/{agent_slug}/versions",
    response_model=list[VersionSummary],
    summary="List an agent's versions",
)
async def list_versions(
    agent_slug: str,
    service: ConfigurationService = Depends(get_configuration_service),
) -> list[VersionSummary]:
    return await service.list_versions(agent_slug)


@router.get(
    "/{agent_slug}/versions/{version}",
    response_model=VersionRead,
    summary="Get one version, with its steps",
)
async def get_version(
    agent_slug: str,
    version: int,
    service: ConfigurationService = Depends(get_configuration_service),
) -> VersionRead:
    return await service.get_version(agent_slug, version)


@router.get(
    "/{agent_slug}/diff",
    response_model=VersionDiff,
    summary="What changed between two versions",
)
async def diff_versions(
    agent_slug: str,
    from_version: int = Query(ge=1, description="The version being compared against"),
    to_version: int = Query(ge=1, description="The version being reviewed"),
    service: ConfigurationService = Depends(get_configuration_service),
) -> VersionDiff:
    """Shaped as a review: what arrived, what left, what moved, what changed.

    Steps that only moved are reported separately from steps that were edited
    -- a reorder and an edit need different attention, and lumping them
    together makes every reorder look like forty edits.
    """

    return await service.diff(agent_slug, from_version, to_version)


# --- Authoring a draft ------------------------------------------------------


@router.post(
    "/{agent_slug}/versions",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new draft version",
)
async def create_version(
    agent_slug: str,
    payload: VersionCreate,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> VersionRead:
    """A draft, optionally cloned from an existing version.

    Cloning is the normal path: a version is almost always the previous one
    with something changed. It copies the steps *and* their tool grants, which
    live in a separate table and are the thing a hand-rolled clone forgets.
    """

    return await service.create_version(agent_slug, payload, actor=identity.actor)


@router.patch(
    "/{agent_slug}/versions/{version}",
    response_model=VersionRead,
    summary="Edit a draft's defaults or notes",
)
async def update_version(
    agent_slug: str,
    version: int,
    payload: VersionUpdate,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> VersionRead:
    return await service.update_version(agent_slug, version, payload, actor=identity.actor)


@router.put(
    "/{agent_slug}/versions/{version}/steps",
    response_model=VersionRead,
    summary="Replace a draft's step list",
)
async def replace_steps(
    agent_slug: str,
    version: int,
    payload: StepsReplace,
    identity: CallerIdentity = Depends(require_role(Role.EDITOR)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> VersionRead:
    """The whole list, in execution order, in one transaction.

    Position is the index rather than a field, and the list is replaced rather
    than patched: a partial update cannot express a reorder without a moment
    where two steps claim one position, which the unique constraint refuses
    half-way through and leaves the draft in neither shape.
    """

    return await service.replace_steps(agent_slug, version, payload, actor=identity.actor)


@router.delete(
    "/{agent_slug}/versions/{version}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def delete_version(
    agent_slug: str,
    version: int,
    identity: CallerIdentity = Depends(require_role(Role.ADMIN)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> None:
    """Drafts only. A frozen version is refused by the database itself."""

    await service.delete_version(agent_slug, version, actor=identity.actor)


# --- Publishing -------------------------------------------------------------


@router.post(
    "/{agent_slug}/versions/{version}/publish",
    response_model=PublishResult,
    summary="Validate and publish a version",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def publish_version(
    agent_slug: str,
    version: int,
    payload: PublishRequest,
    identity: CallerIdentity = Depends(require_role(Role.ADMIN)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> PublishResult:
    """One transaction: validate, freeze, move the pointer, write the audit row.

    A 422 carries *every* problem rather than the first. Forty steps validated
    one refusal at a time is forty round trips, and an author who fixes one
    problem and learns about the next concludes that publish is unpredictable
    when it is being perfectly consistent.

    ``dry_run`` runs the identical path and changes nothing, which is the only
    kind of dry run worth having.
    """

    return await service.publish(agent_slug, version, payload, actor=identity.actor)


@router.post(
    "/{agent_slug}/rollback",
    response_model=PublishResult,
    summary="Point an agent back at an earlier version",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def rollback(
    agent_slug: str,
    payload: RollbackRequest,
    identity: CallerIdentity = Depends(require_role(Role.ADMIN)),
    service: ConfigurationService = Depends(get_configuration_service),
) -> PublishResult:
    """Moving a pointer. No data change, no deletion.

    Deliberately not a re-publish: the target is already frozen and was
    validated when it was published. Re-validating could refuse a rollback
    because a model has since been retired -- which is precisely the moment a
    rollback is most needed.
    """

    return await service.rollback(agent_slug, payload, actor=identity.actor)
