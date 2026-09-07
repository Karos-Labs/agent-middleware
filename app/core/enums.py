"""Enumerations shared by the ORM models and the API schemas.

Values are stored in the database as plain strings, which keeps migrations
simple (no native database enum types to alter) while still giving the API a
closed, documented set of values.
"""

from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    """Lifecycle status of an agent, controlled by the portal."""

    ACTIVE = "active"
    DISABLED = "disabled"
    #: A portal agent with no agent-engine workflow behind it. Distinct from
    #: `disabled`, which is a decision someone made about an agent that works:
    #: this one has nowhere to run. The chat router needs the difference so it
    #: can say "that is still on the old path" instead of failing, and
    #: `get_active` refuses it for the same reason it refuses `disabled`.
    LEGACY_ONLY = "legacy_only"


class Capability(StrEnum):
    """What an agent can be asked to do -- verbs, not descriptions.

    A CLOSED vocabulary, extended only by changing this enum, because the whole
    point is that the chat router selects on capability rather than by matching
    the words in an agent's name. A free-text list would be a description field
    with extra steps: two agents would spell the same ability differently and
    the router would be back to string matching.

    Kept small on purpose. A capability answers "would this agent be the right
    one to ask", not "what exactly does it produce" -- that is the deliverable
    kind, which is C5's business and lives on the wire, not here.
    """

    DRAFT_SOCIAL_POST = "draft_social_post"
    DRAFT_ARTICLE = "draft_article"
    DRAFT_NEWSLETTER = "draft_newsletter"
    DRAFT_REPLY = "draft_reply"
    BUILD_LANDING_PAGE = "build_landing_page"
    PRODUCE_VIDEO = "produce_video"
    PRODUCE_CAROUSEL = "produce_carousel"
    RUN_SEO_AUDIT = "run_seo_audit"
    RUN_INTEL_REPORT = "run_intel_report"
    RUN_SETUP = "run_setup"
    ORCHESTRATE_CAMPAIGN = "orchestrate_campaign"


class TemplateKind(StrEnum):
    """What a content template describes."""

    HTML_LAYOUT = "html_layout"
    JSON_SCHEMA = "json_schema"
    POST = "post"
    LANDING_PAGE = "landing_page"
    EMAIL = "email"
    OTHER = "other"


class ExampleSource(StrEnum):
    """Where a few-shot example came from."""

    MANUAL = "manual"
    FEEDBACK = "feedback"


class RunStatus(StrEnum):
    """Lifecycle of a single agent run (one job handed to the engine)."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class FeedbackStatus(StrEnum):
    """Reviewer verdict attached to a run."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"

class ModelVendor(StrEnum):
    """Who MAKES a model. Distinct from how this deployment reaches it.

    agent-engine has its own ``ModelVendorSchema`` -- ``anthropic`` /
    ``gemini`` / ``model-garden`` / ``openai-compatible`` -- which answers a
    different question and happens to share the word. That one is
    :class:`ModelRoute` here. Llama served through Model Garden is
    ``vendor=meta, route=model-garden``; collapsing the two axes into one
    column is how a model that exists becomes inexpressible.
    """

    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    META = "meta"
    MISTRAL = "mistral"
    OPENAI = "openai"
    OTHER = "other"


class ModelRoute(StrEnum):
    """How this deployment reaches a model -- agent-engine's own vendor axis.

    Mirrors ``ModelVendorSchema`` in ``packages/core/src/types/model-policy.ts``
    exactly, because the router branches on it: each value implies a different
    wire shape, a different structured-output mechanism and a different
    failure mode.
    """

    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    MODEL_GARDEN = "model-garden"
    OPENAI_COMPATIBLE = "openai-compatible"


class ProviderPolicy(StrEnum):
    """Whether a step's model may be substituted on failure.

    Orthogonal to both vendor axes above, and mirrors the engine's
    ``ProviderPolicySchema``. ``pinned`` never swaps -- a pinned step's model
    is what it is, or the step fails loudly.
    """

    PINNED = "pinned"
    PORTABLE = "portable"
    COMMODITY = "commodity"


#: Prompt-cache reads run at roughly a 90% discount off base input price, which
#: is what agent-engine's ``CACHE_READ_DISCOUNT`` applies to every model that
#: supports caching. A catalog row may override it with an explicit
#: ``cached_input_per_1m`` -- some models price cache reads differently, and a
#: multiplier that is right for most of them is still wrong for those.
CACHE_READ_DISCOUNT = 0.1


def route_for_vendor(vendor: ModelVendor) -> ModelRoute:
    """The route a vendor is reached by in this deployment, when unstated.

    A derivation and not a guess: every model in the catalog today is reached
    the way its vendor is reached here. Stating ``route`` explicitly on the row
    overrides it, which is what a deployment that fronts Claude through a
    LiteLLM gateway would do.
    """

    return {
        ModelVendor.ANTHROPIC: ModelRoute.ANTHROPIC,
        ModelVendor.GOOGLE: ModelRoute.GEMINI,
        ModelVendor.META: ModelRoute.MODEL_GARDEN,
        ModelVendor.MISTRAL: ModelRoute.MODEL_GARDEN,
        ModelVendor.OPENAI: ModelRoute.OPENAI_COMPATIBLE,
        ModelVendor.OTHER: ModelRoute.OPENAI_COMPATIBLE,
    }[vendor]


class ModelAvailability(StrEnum):
    """Whether a Studio author may select this model.

    ``not_enabled`` is the interesting one: Vertex offers it, this deployment
    does not route it. Listed and shown disabled rather than hidden, because a
    catalog that silently omits models reads as "this is everything Vertex
    has", and someone concludes a model is unavailable when it is one config
    change away.
    """

    AVAILABLE = "available"
    NOT_ENABLED = "not_enabled"
    RETIRED = "retired"
