"""The normalized model catalog.

Agent stages used to name a model as free text: ``"claude-sonnet-4-6"`` in one
place, ``"claude-sonnet-4-6-on-vertex"`` in another, a typo in a third. Nothing
told a Studio author which strings the engine actually routes, and nothing
stopped a stage naming one it does not.

A ``models`` collection makes that a reference rather than a spelling. The
document id is the ``model_id`` a stage stores, which is what turns "does this
model exist" into a lookup instead of a guess.

Two states are deliberately distinct:

* ``available`` -- Vertex offers it AND this deployment routes it. Selectable.
* ``not_enabled`` -- Vertex offers it, the engine does not route it here.
  Listed, shown disabled, and requestable. Hiding it would make the catalog
  look like the whole of what Vertex has, which is how someone concludes a
  model is unavailable when it is one config change away.

``retired`` exists so a model a stage already references can stop being
selectable without the reference dangling -- an old run's recorded model must
still resolve to something that explains what it was.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.core.enums import ModelAvailability, ModelVendor

ModelIdStr = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9.\-]*$",
        description=(
            "Stable, URL-safe identifier and the Firestore document id. Also what an "
            "agent stage stores as its modelId."
        ),
    ),
]


class ModelCreate(BaseModel):
    """Body for ``POST /models``."""

    model_id: ModelIdStr
    display_name: str = Field(min_length=1, max_length=255)
    vendor: ModelVendor
    availability: ModelAvailability = ModelAvailability.AVAILABLE
    #: What the vendor's own API expects, which is NOT always the document id --
    #: Claude on Vertex is published under a different string from Claude on
    #: Anthropic's own API, and the engine needs the one its router will send.
    provider_model_name: str = Field(min_length=1, max_length=200)
    #: Vertex publisher location, when the vendor is served through Vertex.
    region: str | None = Field(default=None, max_length=64)
    description: str | None = None
    #: Rough capability hints a Studio author picks on, not a billing contract.
    context_window: int | None = Field(default=None, ge=0)
    supports_tools: bool = True
    #: The tier this model is a sensible default for -- mirrors agent-engine's
    #: own pinned/portable/commodity policy vocabulary.
    tiers: list[str] = Field(default_factory=list)
    notes: str | None = None
    #: What the engine does when the primary route for this model fails, in
    #: words a Studio author can act on. Claude on Vertex falls over to the
    #: same model on Anthropic's direct API and then to Gemini 2.5 Flash;
    #: Gemini has no second transport, so a failure fails the step. A model
    #: whose fallback is undocumented is one whose failure mode nobody chose.
    fallback: str | None = None


class ModelUpdate(BaseModel):
    """Body for ``PATCH /models/{model_id}``; unset fields are left untouched."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    availability: ModelAvailability | None = None
    provider_model_name: str | None = Field(default=None, min_length=1, max_length=200)
    region: str | None = Field(default=None, max_length=64)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=0)
    supports_tools: bool | None = None
    tiers: list[str] | None = None
    notes: str | None = None
    fallback: str | None = None


class ModelRead(BaseModel):
    """A model as returned by the API."""

    id: str
    model_id: str
    display_name: str
    vendor: ModelVendor
    availability: ModelAvailability
    provider_model_name: str
    region: str | None
    description: str | None
    context_window: int | None
    supports_tools: bool
    tiers: list[str]
    notes: str | None
    #: Absent on rows seeded before the field existed; the Studio shows those as "not documented".
    fallback: str | None = None
    created_at: datetime
    updated_at: datetime


class ModelAccessRequest(BaseModel):
    """Body for ``POST /models/{model_id}/access-request``.

    Recorded rather than actioned. Enabling a model is a deployment decision --
    the engine has to route it and someone has to accept the cost -- so this
    captures who asked and why, and a human does the enabling.
    """

    requested_by: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    agent_id: str | None = Field(
        default=None, max_length=128, description="The agent the requester wanted it for"
    )


class ModelAccessRequestRead(BaseModel):
    id: str
    model_id: str
    requested_by: str
    reason: str | None
    agent_id: str | None
    status: str
    created_at: datetime


def model_document(payload: ModelCreate, now: datetime) -> dict[str, Any]:
    """The stored shape. Kept beside the schema so the two cannot drift."""

    return {
        "id": payload.model_id,
        "model_id": payload.model_id,
        "display_name": payload.display_name,
        "vendor": payload.vendor.value,
        "availability": payload.availability.value,
        "provider_model_name": payload.provider_model_name,
        "region": payload.region,
        "description": payload.description,
        "context_window": payload.context_window,
        "supports_tools": payload.supports_tools,
        "tiers": payload.tiers,
        "notes": payload.notes,
        "fallback": payload.fallback,
        "created_at": now,
        "updated_at": now,
    }
