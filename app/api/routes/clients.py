"""Client-context projection: the portal's way into the engine workspace.

Before this, all four populators were CLIs a person ran by hand -- argparse,
``--env``, ``--confirm``. There was no scheduler, no webhook and no caller from
the portal, so the engine's workspace was a hand-seeded copy that aged freely
and nobody could say by how much.

Two routes, and the split matters. The POST is what the portal calls when
somebody saves a context document, so the copy an agent reads stops drifting
from the document a human edited. The GET is the freshness signature: how far
the copy has drifted, per document, for a readiness report or a cutover check
that has this API and no bucket access.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.schemas.client_context import (
    DocumentOutcomeRead,
    FreshnessRead,
    ProjectedDocumentRead,
    ProjectionRead,
)
from app.api.schemas.common import SlugStr
from app.core.roles import Role
from app.dependencies import get_projector
from app.security import require_role
from app.services.client_context import ClientContextProjector

router = APIRouter(prefix="/clients", tags=["clients"])

_UNCONFIGURED = (
    "Client-context projection is unavailable: GCS_ARTIFACTS_BUCKET is not configured "
    "for this deployment."
)


def _require_projector(
    projector: ClientContextProjector | None = Depends(get_projector),
) -> ClientContextProjector:
    """503 with the variable named, rather than a stack trace from a client.

    Locally there is no bucket, and refusing to start without one would make
    everyone configure GCS to work on the agent CRUD they were actually
    touching. So the service starts and only these two routes are unavailable.
    """

    if projector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNCONFIGURED
        )
    return projector


@router.post(
    "/{client_slug}/context/reseed",
    response_model=ProjectionRead,
    summary="Project one client's context documents into the engine workspace",
    description=(
        "Idempotent by content hash: a save that did not change the text writes nothing and "
        "leaves `projectedAt` alone. Safe to call on every document save."
    ),
    dependencies=[Depends(require_role(Role.EDITOR))],
)
async def reseed_client_context(
    client_slug: SlugStr,
    projected_by: Annotated[
        str,
        Query(
            description=(
                "Recorded in each record's provenance so the audit trail can name the "
                "mechanism. The reader never branches on it."
            )
        ),
    ] = "portal-save",
    projector: ClientContextProjector = Depends(_require_projector),
) -> ProjectionRead:
    result = await projector.project(client_slug, projected_by=projected_by)
    return ProjectionRead(
        slug=result.slug,
        written=result.written,
        documents=[DocumentOutcomeRead(**vars(d)) for d in result.documents],
        competitors=(
            DocumentOutcomeRead(**vars(result.competitors)) if result.competitors else None
        ),
    )


@router.get(
    "/{client_slug}/context",
    response_model=FreshnessRead,
    summary="How fresh the projected copy is, per document",
)
async def read_client_context_freshness(
    client_slug: SlugStr,
    projector: ClientContextProjector = Depends(_require_projector),
) -> FreshnessRead:
    documents = await projector.freshness(client_slug)
    rows = [
        ProjectedDocumentRead(
            doc_type=d.doc_type,
            projected_version=d.projected_version,
            current_version=d.current_version,
            projected_at=d.projected_at,
            state=d.state,  # type: ignore[arg-type]
        )
        for d in documents
    ]
    return FreshnessRead(
        slug=client_slug,
        documents=rows,
        # `unprojectable` does not count against currency: it is a deliberate
        # exclusion, not a gap somebody has to close.
        is_current=all(r.state in ("fresh", "unprojectable") for r in rows),
    )
