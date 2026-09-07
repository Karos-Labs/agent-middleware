"""Re-seeding a client's context, and reporting how fresh it is."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DocumentOutcomeRead(BaseModel):
    """What happened to one record in a projection."""

    doc_type: str
    outcome: Literal["created", "updated", "unchanged", "skipped"]
    detail: str = ""
    #: ``ClientContextDoc.version`` at the moment it was projected. Null for
    #: competitors, which are rows rather than a versioned document.
    doc_version: int | None = None


class ProjectionRead(BaseModel):
    """Body of ``POST /clients/{slug}/context/reseed``."""

    slug: str
    #: How many records were actually written. Zero is a perfectly good answer
    #: and the common one: the projection is idempotent by content hash, so a
    #: save that did not change the text writes nothing.
    written: int
    documents: list[DocumentOutcomeRead] = Field(default_factory=list)
    competitors: DocumentOutcomeRead | None = None


class ProjectedDocumentRead(BaseModel):
    """One document's freshness."""

    doc_type: str
    #: ``docVersion`` recorded in the workspace copy. Null when nothing is
    #: projected.
    projected_version: int | None
    #: ``version`` in Firestore right now. Null when the document does not
    #: exist at the ``internal`` tier.
    current_version: int | None
    projected_at: str | None
    #: ``fresh`` the agent reads the current text · ``stale`` the portal has
    #: moved on · ``absent`` nothing is projected · ``unprojectable`` the
    #: document exists only at a tier this never projects, so no write is
    #: coming and there is nothing to chase.
    state: Literal["fresh", "stale", "absent", "unprojectable"]


class FreshnessRead(BaseModel):
    """Body of ``GET /clients/{slug}/context``."""

    slug: str
    documents: list[ProjectedDocumentRead] = Field(default_factory=list)
    #: True when every projected document is current. What a readiness report
    #: or a cutover check actually wants to know, so it is computed here rather
    #: than re-derived by each caller.
    is_current: bool
