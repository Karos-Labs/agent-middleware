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

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from app.core.enums import (
    CACHE_READ_DISCOUNT,
    ModelAvailability,
    ModelRoute,
    ModelVendor,
    ProviderPolicy,
    route_for_vendor,
)

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
    """Body for ``POST /models``.

    ``input_per_1m``, ``output_per_1m`` and ``pricing_checked_on`` are
    REQUIRED, and that is a deliberate break with the previous shape (S12 /
    SCRUM-222). A model with no price is exactly the row that made
    ``pricingForModel`` fall through to ``DEFAULT_MODEL_PRICING`` -- Sonnet's
    $3/$15 -- silently, for every model it did not recognise. Requiring the
    price here means the catalog cannot hold the row that causes it.
    """

    model_id: ModelIdStr
    display_name: str = Field(min_length=1, max_length=255)
    vendor: ModelVendor
    #: How THIS deployment reaches it -- agent-engine's own vendor axis, which
    #: is a different question from ``vendor`` above. Derived from the vendor
    #: when omitted; state it when the deployment differs from the default.
    route: ModelRoute | None = None
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

    # --- pricing, required ------------------------------------------------
    #: USD per 1M input tokens.
    input_per_1m: float = Field(ge=0)
    #: USD per 1M output tokens.
    output_per_1m: float = Field(ge=0)
    #: USD per 1M cached input tokens. Omit to take CACHE_READ_DISCOUNT x
    #: input, which is what the engine already assumes; state it for a model
    #: that prices cache reads on its own multiplier.
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    #: Where the two numbers came from. Free text on purpose -- a URL, a
    #: contract reference, "quoted by the vendor" -- because the useful thing
    #: is being able to go back to the same place.
    pricing_source: str = Field(default="vendor_price_list", max_length=500)
    #: The day the price was read off that source. Required, because "we do not
    #: know how old this number is" is the state the engine's hard-coded table
    #: is in: it carries no date at all, and two of its Claude rows have been
    #: wrong by 3x since the vendor cut its prices.
    pricing_checked_on: date

    @model_validator(mode="after")
    def _cache_read_cannot_exceed_input(self) -> ModelCreate:
        if (
            self.cached_input_per_1m is not None
            and self.cached_input_per_1m > self.input_per_1m
        ):
            raise ValueError(
                "cached_input_per_1m must not exceed input_per_1m: a cache read that "
                "costs more than a fresh read is a transcription error, and it would "
                "make every cached call look more expensive than not caching"
            )
        return self

    @property
    def resolved_route(self) -> ModelRoute:
        return self.route or route_for_vendor(self.vendor)


class ModelUpdate(BaseModel):
    """Body for ``PATCH /models/{model_id}``; unset fields are left untouched."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    route: ModelRoute | None = None
    availability: ModelAvailability | None = None
    provider_model_name: str | None = Field(default=None, min_length=1, max_length=200)
    region: str | None = Field(default=None, max_length=64)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=0)
    supports_tools: bool | None = None
    tiers: list[str] | None = None
    notes: str | None = None
    input_per_1m: float | None = Field(default=None, ge=0)
    output_per_1m: float | None = Field(default=None, ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    pricing_source: str | None = Field(default=None, max_length=500)
    pricing_checked_on: date | None = None

    @model_validator(mode="after")
    def _a_price_change_restates_its_date(self) -> ModelUpdate:
        """Changing a price without saying when it was checked is not allowed.

        The date is the only thing that distinguishes a current price from one
        nobody has looked at since 2024, and a patch that moves the number and
        leaves the date is worse than one that does neither: it makes a stale
        row look fresh.
        """

        changed = self.model_fields_set
        prices = {"input_per_1m", "output_per_1m", "cached_input_per_1m"}
        if changed & prices and "pricing_checked_on" not in changed:
            raise ValueError(
                "a price change must restate pricing_checked_on -- otherwise the row "
                "carries a new number under an old date"
            )
        return self


class ModelRead(BaseModel):
    """A model as returned by the API."""

    id: str
    model_id: str
    display_name: str
    vendor: ModelVendor
    #: Null on rows written before S12; treated as ``route_for_vendor(vendor)``
    #: by every reader, which is what those rows already meant.
    route: ModelRoute | None = None
    availability: ModelAvailability
    provider_model_name: str
    region: str | None
    description: str | None
    context_window: int | None
    supports_tools: bool
    tiers: list[str]
    notes: str | None
    #: Null on rows written before S12. A null here is the whole bug: it is a
    #: model an agent can name and nothing can price.
    input_per_1m: float | None = None
    output_per_1m: float | None = None
    cached_input_per_1m: float | None = None
    pricing_source: str | None = None
    pricing_checked_on: date | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_priced(self) -> bool:
        return self.input_per_1m is not None and self.output_per_1m is not None

    @property
    def cache_read_rate(self) -> float | None:
        """USD per 1M cached input tokens, explicit or derived."""

        if self.cached_input_per_1m is not None:
            return self.cached_input_per_1m
        if self.input_per_1m is None:
            return None
        return self.input_per_1m * CACHE_READ_DISCOUNT


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
        "route": payload.resolved_route.value,
        "input_per_1m": payload.input_per_1m,
        "output_per_1m": payload.output_per_1m,
        "cached_input_per_1m": payload.cached_input_per_1m,
        "pricing_source": payload.pricing_source,
        "pricing_checked_on": payload.pricing_checked_on.isoformat(),
        "created_at": now,
        "updated_at": now,
    }


# --- Aliases ----------------------------------------------------------------


AliasStr = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="The alias a Studio author picks, e.g. `sonnet`",
    ),
]


class ModelAliasUpsert(BaseModel):
    """Body for ``PUT /models/aliases/{alias}``.

    ``MODEL_ALIASES`` is three ``as const`` lines in
    ``packages/core/src/router/aliases.ts``, which means a new Sonnet
    generation is a code change and a redeploy -- precisely what an alias
    exists to prevent. This makes it a row.
    """

    model_id: ModelIdStr
    provider_policy: ProviderPolicy
    description: str | None = Field(default=None, max_length=500)


class ModelAliasRead(BaseModel):
    """An alias and what it currently points at."""

    alias: str
    model_id: str
    provider_policy: ProviderPolicy
    description: str | None
    #: Denormalized for the one question every caller has next.
    provider_model_name: str | None = None
    region: str | None = None
    created_at: datetime
    updated_at: datetime


def alias_document(alias: str, payload: ModelAliasUpsert, now: datetime) -> dict[str, Any]:
    return {
        "id": alias,
        "alias": alias,
        "model_id": payload.model_id,
        "provider_policy": payload.provider_policy.value,
        "description": payload.description,
        "created_at": now,
        "updated_at": now,
    }


# --- Pricing coverage -------------------------------------------------------


class UnpricedModelReference(BaseModel):
    """One place an agent names a model the catalog cannot price."""

    model_id: str
    agent_id: str
    agent_slug: str
    #: The stage that names it, or null when it is the agent's own default.
    stage_id: str | None
    #: ``missing`` -- no catalog row at all. ``unpriced`` -- a row with no
    #: price, which is a row written before S12.
    reason: str


class PricingCoverage(BaseModel):
    """Whether every model any agent names can be priced.

    The pre-flight for turning enforcement on: an environment whose catalog is
    not yet seeded reports its gaps here instead of discovering them one
    refused dispatch at a time.
    """

    #: Distinct model ids named by any agent, at agent or stage level.
    referenced_models: list[str]
    priced_models: list[str]
    gaps: list[UnpricedModelReference]

    @property
    def is_complete(self) -> bool:
        return not self.gaps
