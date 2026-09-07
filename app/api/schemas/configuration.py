"""The Configuration API's shapes — agent versions, steps, publish and diff.

An agent *version* is the unit this API exists for: a whole workflow graph,
every prompt body it uses, every model and tool it names, frozen together so
that "which configuration produced this deliverable" has an answer (C6).

Two things about these shapes are deliberate and worth reading before adding
to them.

**A version is written as a whole, not field by field.** ``PUT .../steps``
replaces the step list. A per-step PATCH surface would let a caller leave a
draft in a state no publish would accept and no reader could explain -- step 7
renamed, step 9 still referencing the old id -- and the fix for that is a
transaction, which is what replacing the list already is.

**Publish reports every problem, not the first.** A 40-step version validated
one refusal at a time is forty round trips. :class:`ValidationProblem` is a
list for that reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

#: agent-engine's ProviderPolicySchema: whether a step's model may be
#: substituted on failure. `pinned` never swaps -- a pinned step's model is
#: what it is, or the step fails loudly.
#:
#: A Literal here rather than an import, because S12 (SCRUM-222) introduces the
#: same vocabulary as `app.core.enums.ProviderPolicy` on its own branch and
#: each branch has to pass CI on its own. Collapse the two onto the enum once
#: both are in dev-shlomi -- it is a one-line change and a note on S4.
ProviderPolicyLiteral = Literal["pinned", "portable", "commodity"]

#: Same charset as agent-engine's own StageIdSchema and the S2 `config.slug`
#: domain, so a step id is the same string in the database, the API and the
#: engine's checkpoint keys.
StepIdStr = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
]

StepKind = Literal["ai", "code", "gate"]
GateKind = Literal["batch_review", "prompt_set_review", "fix_generation_review"]
CodeLanguage = Literal["node", "python"]

#: The flat output DSL agent-engine's `AgentDefinitionFieldSchema` supports.
#: Not arbitrary JSON Schema: translating that into Zod at runtime is a real
#: piece of engineering the engine does not attempt, so accepting it here would
#: be accepting something no consumer can honour.
OutputFieldType = Literal["string", "number", "boolean", "string[]"]

MAX_STAGE_CODE_CHARS = 20_000


class OutputField(BaseModel):
    """One field a model turn is required to return."""

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="A valid identifier, because the engine builds a Zod shape from it",
    )
    type: OutputFieldType
    description: str | None = Field(default=None, max_length=500)
    optional: bool = False


class SelfCritique(BaseModel):
    """The gate loop a drafting step runs against its own output."""

    gate_tool: str = Field(min_length=1, max_length=128)
    max_revisions: int = Field(default=1, ge=1, le=5)
    #: Static fields merged onto the draft before the gate sees it, winning
    #: over anything the model happened to include.
    gate_args: dict[str, Any] = Field(default_factory=dict)


class StepBounds(BaseModel):
    """The engine's own ceilings, under the engine's own names.

    Not "retry" and "timeout". agent-engine has no per-step attempt count and
    no per-step backoff, and its agent-step timeout is per RUN -- see
    ``VersionDefaults.agent_step_timeout_ms``. Naming a field for a concept the
    consumer does not have is how a wrong implementation grows to match a
    schema (C6 §9.1).
    """

    max_steps: int | None = Field(
        default=None, ge=1, le=64, description="ReAct turn ceiling; the engine defaults to 8"
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Output-token ceiling for one turn. A turn that runs out of room returns "
            "truncated, unparseable structured output and the step fails outright, so "
            "this is not a soft limit."
        ),
    )
    max_malformed_turns: int | None = Field(
        default=None,
        ge=0,
        le=5,
        description=(
            "Malformed turns re-prompted with their own validation error before giving "
            "up; the engine defaults to 1. Bounded, because a model looping on the same "
            "shape mistake is a config problem to surface, not a budget to spend."
        ),
    )


class VersionStepWrite(BaseModel):
    """One step, as authored."""

    step_id: StepIdStr
    kind: StepKind
    description: str = Field(default="", max_length=2000)

    # --- ai steps ---------------------------------------------------------
    #: Either name a prompt version by (key, version) or give its id outright.
    #: (key, version) is what a human writes; the id is what a clone passes
    #: back unchanged.
    prompt_key: str | None = Field(default=None, max_length=256)
    prompt_version: int | None = Field(default=None, ge=1)
    prompt_version_id: str | None = None
    model_id: str | None = Field(default=None, max_length=128)
    provider_policy: ProviderPolicyLiteral | None = None
    fallback_model_id: str | None = Field(default=None, max_length=128)
    output_schema: list[OutputField] | None = None
    bounds: StepBounds = Field(default_factory=StepBounds)
    self_critique: SelfCritique | None = None

    # --- code steps -------------------------------------------------------
    language: CodeLanguage | None = None
    code: str | None = Field(default=None, max_length=MAX_STAGE_CODE_CHARS)
    code_timeout_ms: int | None = Field(default=None, ge=1)

    # --- gate steps -------------------------------------------------------
    gate_kind: GateKind | None = None

    # --- any step ---------------------------------------------------------
    skill_ref: str | None = Field(default=None, max_length=200)
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tool codes this step may call. Narrow and explicit, never a blanket "
            "allowlist."
        ),
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_way_of_naming_a_prompt(self) -> VersionStepWrite:
        named_by_key = self.prompt_key is not None or self.prompt_version is not None
        if named_by_key and self.prompt_version_id is not None:
            raise ValueError(
                "name a prompt either by (prompt_key, prompt_version) or by "
                "prompt_version_id, not both -- two answers to 'which prompt' is a "
                "question the server would have to guess at"
            )
        if named_by_key and (self.prompt_key is None or self.prompt_version is None):
            raise ValueError(
                "prompt_key and prompt_version go together: a key with no version "
                "means 'the latest', and a step that means 'the latest' is a step "
                "whose behaviour changes when someone edits a prompt"
            )
        return self

    @model_validator(mode="after")
    def _a_pinned_step_has_no_fallback(self) -> VersionStepWrite:
        # agent-engine rejects a fallback declared alongside `pinned` rather
        # than ignoring it: a pinned step's model is what it is, or the step
        # fails loudly. The database says the same; so does this.
        if self.fallback_model_id and (self.provider_policy or "pinned") == "pinned":
            raise ValueError(
                "a pinned step cannot declare a fallback model -- pinned means it "
                "never silently substitutes one"
            )
        return self

    @model_validator(mode="after")
    def _a_sandbox_timeout_needs_a_script(self) -> VersionStepWrite:
        if self.code_timeout_ms is not None and self.code is None:
            raise ValueError(
                "code_timeout_ms is the sandbox's budget for a script; on a step with "
                "no script it is a number nothing reads"
            )
        return self


class VersionStepRead(BaseModel):
    """One step, as stored, with the references resolved for display."""

    step_id: str
    position: int
    kind: str
    description: str
    prompt_key: str | None = None
    prompt_version: int | None = None
    prompt_version_id: str | None = None
    prompt_content_hash: str | None = None
    model_id: str | None = None
    provider_policy: str | None = None
    fallback_model_id: str | None = None
    output_schema: list[OutputField] | None = None
    bounds: StepBounds = Field(default_factory=StepBounds)
    self_critique: SelfCritique | None = None
    language: str | None = None
    code: str | None = None
    code_timeout_ms: int | None = None
    is_gate: bool = False
    gate_kind: str | None = None
    skill_ref: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class VersionDefaults(BaseModel):
    """Version-level settings every step inherits unless it overrides them."""

    default_model_id: str | None = Field(default=None, max_length=128)
    default_provider_policy: ProviderPolicyLiteral = "pinned"
    dedupe_against_history: bool = False
    agent_step_timeout_ms: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Overrides the engine's 10-minute default for every step.agent call in a "
            "run of this version. Per-run and not per-step, because that is the only "
            "shape WorkflowRuntime accepts."
        ),
    )


class VersionCreate(BaseModel):
    """Body for ``POST /config/agents/{slug}/versions``."""

    #: Clone an existing version's defaults and steps into the new draft.
    #: The normal way a version is authored: the previous one, edited.
    from_version: int | None = Field(default=None, ge=1)
    defaults: VersionDefaults | None = None
    notes: str | None = Field(default=None, max_length=2000)


class VersionUpdate(BaseModel):
    """Body for ``PATCH /config/agents/{slug}/versions/{version}``. Drafts only."""

    defaults: VersionDefaults | None = None
    notes: str | None = Field(default=None, max_length=2000)


class VersionSummary(BaseModel):
    """A version without its steps, for a listing."""

    id: str
    agent_slug: str
    version: int
    status: Literal["draft", "frozen"]
    #: draft / published / superseded, derived from the pointer -- there are
    #: only two stored statuses, and this is the third state a reader wants.
    lifecycle: Literal["draft", "published", "superseded"]
    step_count: int
    notes: str | None
    frozen_at: datetime | None
    frozen_by: str | None
    created_at: datetime
    created_by: str | None


class VersionRead(VersionSummary):
    """A version and everything in it."""

    defaults: VersionDefaults
    steps: list[VersionStepRead]


class StepsReplace(BaseModel):
    """Body for ``PUT /config/agents/{slug}/versions/{version}/steps``.

    The whole list, in execution order. Position is the index rather than a
    field: two sources of truth for order is how a reordered draft ends up
    running in the order it was written instead of the order it is displayed.
    """

    steps: list[VersionStepWrite] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> StepsReplace:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(
                    f"step id '{step.step_id}' appears twice; ids are how a reference "
                    "and a checkpoint find a step, so a duplicate makes both ambiguous"
                )
            seen.add(step.step_id)
        return self


class ValidationProblem(BaseModel):
    """One reason a version cannot be published."""

    code: str = Field(description="Stable machine-readable reason, e.g. `unpriced_model`")
    message: str
    step_id: str | None = None
    field: str | None = None


class PublishRequest(BaseModel):
    """Body for ``POST /config/agents/{slug}/versions/{version}/publish``."""

    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Recorded on the audit row. What this release is for.",
    )
    #: Validate and report, changing nothing. The same code path as a real
    #: publish, which is the only kind of dry run worth having.
    dry_run: bool = False


class PublishResult(BaseModel):
    """What a publish did."""

    agent_slug: str
    version: int
    version_id: str
    #: What the agent pointed at before. Null the first time an agent is
    #: published, which is also how a reader tells a first release from a
    #: replacement.
    previous_version_id: str | None
    previous_version: int | None
    published_at: datetime
    published_by: str
    audit_id: int | None = None
    dry_run: bool = False
    #: Empty on a successful publish. Populated on a dry run that would fail.
    problems: list[ValidationProblem] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    """Body for ``POST /config/agents/{slug}/rollback``."""

    to_version: int = Field(ge=1)
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Recorded on the audit row. A rollback with no recorded reason is the one "
            "everybody argues about a week later."
        ),
    )


class FieldChange(BaseModel):
    """One field that differs between two versions of a step."""

    field: str
    before: Any = None
    after: Any = None


class StepChange(BaseModel):
    """One step that differs, and how."""

    step_id: str
    position_before: int | None = None
    position_after: int | None = None
    fields: list[FieldChange] = Field(default_factory=list)
    #: A unified diff of the prompt bodies, when the prompt version changed.
    #: The thing a reviewer actually wants to see, and the only field here that
    #: can be large -- capped, with `prompt_diff_truncated` saying so.
    prompt_diff: str | None = None
    prompt_diff_truncated: bool = False


class VersionDiff(BaseModel):
    """What changed between two versions of one agent.

    Shaped as a review rather than a dump: which steps arrived, which left,
    which moved, and for the ones that stayed, only the fields that differ.
    """

    agent_slug: str
    from_version: int
    to_version: int
    defaults: list[FieldChange] = Field(default_factory=list)
    steps_added: list[str] = Field(default_factory=list)
    steps_removed: list[str] = Field(default_factory=list)
    steps_changed: list[StepChange] = Field(default_factory=list)
    #: Steps whose only change is where they sit in the order. Separate from
    #: `steps_changed` because a reorder and an edit need different attention.
    steps_moved: list[StepChange] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.defaults
            or self.steps_added
            or self.steps_removed
            or self.steps_changed
            or self.steps_moved
        )
