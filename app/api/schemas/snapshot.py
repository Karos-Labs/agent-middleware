"""The ExecutionSnapshot — C6's wire shape.

A run carries the configuration it will use. The engine resolves nothing at run
time, and the control plane's database is not on the run path.

Everything here follows from that one sentence. If the engine resolved a prompt
body or a model id, editing that thing would change a run already in flight,
and "which configuration produced this deliverable" would be answerable only by
reconstructing what the database happened to contain at the time.

These models are the PRODUCER half of a contract whose consumer is in another
repository, so they are deliberately camelCase on the wire: agent-engine's Zod
schemas read `providerModelName`, not `provider_model_name`, and a snapshot the
engine cannot parse is worse than no snapshot at all. Python-side names stay
snake_case; the alias generator bridges them in one place rather than at forty
call sites.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when the shape changes in a way a consumer must know about. The
#: engine REFUSES an unknown version rather than best-effort parsing it (C6
#: §7.3): a consumer that skips fields it does not recognise will run a newer
#: snapshot with its selfCritique block silently dropped, and produce a
#: deliverable that passed no gate.
SNAPSHOT_SCHEMA_VERSION = 1


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class WireModel(BaseModel):
    """camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=_camel, populate_by_name=True, protected_namespaces=()
    )


class SnapshotPricing(WireModel):
    """The price that was in effect when this snapshot was resolved.

    Travels so the engine never looks a price up (C6 §7.5). `pricingForModel`
    falls back to Sonnet's $3/$15 on a miss, silently -- which bills Opus work
    at a third of its cost. With the price here there is nothing to miss, and a
    completed run's cost stops changing when a vendor updates a number.
    """

    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None = None


class SnapshotModel(WireModel):
    """A model resolved all the way down. No alias, no lookup."""

    model_id: str
    provider_model_name: str
    #: Who makes it (agent-middleware's ModelVendor).
    vendor: str
    #: How THIS deployment reaches it (agent-engine's own vendor axis). Two
    #: axes under two names, because the repos use the same word for different
    #: questions and collapsing them leaves a Model Garden model inexpressible.
    route: str
    region: str | None = None
    provider_policy: str = "pinned"
    #: Null whenever provider_policy is "pinned" -- a pinned step's model is
    #: what it is, or the step fails loudly.
    fallback_model: str | None = None
    pricing: SnapshotPricing


class SnapshotPrompt(WireModel):
    """The prompt BODY, not a reference to one."""

    prompt_key: str
    prompt_version_id: str
    version: int
    #: sha256 of `content`. Not for the engine to re-check every run -- so that
    #: "the prompt that produced this deliverable" is a verifiable claim months
    #: later, when the snapshot is the only surviving artifact.
    content_hash: str
    content: str


class SnapshotBounds(WireModel):
    """The engine's own ceilings, under the engine's own names (C6 §9.1)."""

    max_steps: int | None = None
    max_tokens: int | None = None
    max_malformed_turns: int | None = None


class SnapshotSelfCritique(WireModel):
    gate_tool: str
    max_revisions: int = 1
    gate_args: dict[str, Any] = Field(default_factory=dict)


class SnapshotTool(WireModel):
    """One tool a step may call, with the version telemetry records."""

    name: str
    version: str
    config: dict[str, Any] = Field(default_factory=dict)


class SnapshotStep(WireModel):
    """One step, frozen."""

    step_id: str
    position: int
    kind: Literal["ai", "code", "gate"]
    description: str = ""

    prompt: SnapshotPrompt | None = None
    model: SnapshotModel | None = None
    allowed_tools: list[SnapshotTool] = Field(default_factory=list)
    output_schema: list[dict[str, Any]] | None = None
    bounds: SnapshotBounds = Field(default_factory=SnapshotBounds)
    self_critique: SnapshotSelfCritique | None = None

    language: str | None = None
    code: str | None = None
    code_timeout_ms: int | None = None

    is_gate: bool = False
    gate_kind: str | None = None
    skill_ref: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class SnapshotDefaults(WireModel):
    model_id: str | None = None
    provider_policy: str = "pinned"
    #: Per-RUN, not per-step: WorkflowRuntime applies one timeout to every
    #: step.agent call in a run, and that is the only shape it accepts.
    agent_step_timeout_ms: int | None = None
    dedupe_against_history: bool = False


class ExecutionSnapshot(WireModel):
    """Everything a run needs, frozen at dispatch."""

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: str
    #: Must equal the message's `productId`. Redundant on purpose: it means a
    #: snapshot attached to the wrong message is caught rather than executed.
    agent_slug: str
    agent_version_id: str
    agent_version: int
    resolved_at: datetime
    #: "published" | "client_pinned" -- because "why did this client get
    #: different output from that one" is otherwise a question requiring
    #: database archaeology at exactly the moment somebody is annoyed.
    resolved_from: Literal["published", "client_pinned"]
    client_slug: str
    agent_class: str
    capabilities: list[str] = Field(default_factory=list)
    defaults: SnapshotDefaults
    steps: list[SnapshotStep]

    def byte_size(self) -> int:
        """Serialized size, as it will travel."""

        return len(self.model_dump_json(by_alias=True).encode("utf-8"))


class SnapshotReference(WireModel):
    """A snapshot too large to travel inline, and the hash that proves it.

    The hash is not ceremony. A truncated GCS read is valid JSON with fewer
    steps in it, and a run that quietly skips its last stage produces a
    deliverable that looks finished.
    """

    snapshot_id: str
    uri: str
    sha256: str
    byte_size: int
