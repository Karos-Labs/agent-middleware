"""One-way import from the registries agent configuration is scattered across.

S5 / SCRUM-220. The ticket names five: ``customAgents``,
``dynamicAgentSpecs``, ``agentDefinitions``, this service's own ``agents``, and
``prompts``/``promptVersions``. Two of those five turn out not to need reading
at all, and one of them cannot be imported as steps. Both findings are the
point of this module, so they are stated here rather than in a report.

## Where the five actually are

* **this service's ``agents``** — the primary source. One ``config.agents``
  row per document: slug, class, capabilities, platforms, the C4 descriptor
  fields. Read directly.
* **``customAgents``** (portal) — already mirrored onto
  ``agents.custom_agent_keys`` by S-A16, which C4 §7.2 makes the source of
  truth for it. Imported from that field, NOT by reaching into the portal's
  own collection: a control plane that reads another service's private
  collection has quietly made that collection an interface.
* **``prompts``/``promptVersions``** — S7's :class:`UnifiedPromptStore` already
  owns this import, append-only and with the engine's document as a
  projection. Delegated to it rather than reimplemented, because two importers
  writing the same table with different ideas about versioning is the failure
  this whole programme is about.
* **``agentDefinitions``** (engine) and **``dynamicAgentSpecs``** (portal) —
  the dynamic-agent stores. Their stages carry a real ``systemPrompt``, output
  schema or script, so they import as steps. These are the only two that do.

## The one that cannot be imported as steps, and why that is not a gap

A step here must satisfy its kind: an ``ai`` step needs a prompt version and a
non-empty output schema, a ``code`` step needs code and a language
(``agent_version_steps_20_kind_guard``). That is the value of the table.

The thirteen hand-written agent-engine workflows cannot satisfy it. Their stage
list is compiled TypeScript, mirrored into Firestore for display: of 291
recorded stages, 27 carry a skillRef and none carries code, because the code is
a program. Importing them would mean inventing a prompt, a schema or a script
for roughly 260 stages -- and a version assembled that way is not a record of
what runs. It is a plausible guess that S6's resolver would freeze and hand to
the engine as fact, which is worse than having no version at all: the whole
promise of the snapshot is that it says what actually executed.

So they get an agent row, their portal keys and their prompts -- all of which
ARE configuration -- and ``stage_source = 'engine_code'``, which 0005's trigger
turns into a refusal to publish rather than a note.

## Reversible, in the sense the ticket means

Nothing here writes a Firestore document. Prompt bodies are read from
``promptVersions`` and recorded; the projection back is S7's and writes the
same content it read. So dropping this schema leaves the documents as the
source of truth exactly as they are now, and the way to check that claim is
the test that asserts the importer performs no Firestore write at all.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from app.api.schemas.configuration import (
    OutputField,
    StepsReplace,
    VersionCreate,
    VersionDefaults,
    VersionStepWrite,
)
from app.core.exceptions import ResourceConflictError, ValidationRefusedError
from app.db.firestore import AGENTS, FirestoreDB, snapshot_to_dict
from app.db.postgres import ConfigDatabase
from app.services.configuration import ConfigurationService
from app.services.prompt_store import UnifiedPromptStore

logger = logging.getLogger(__name__)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

#: agent-engine's dynamic-agent store, and the portal's. Named here rather than
#: in `app/db/firestore.py` because this module is the only reader: they belong
#: to other services, and importing from them once is different from treating
#: them as this service's collections.
AGENT_DEFINITIONS = "agentDefinitions"
DYNAMIC_AGENT_SPECS = "dynamicAgentSpecs"

#: The engine's word for a model step, on a stored stage. `presentation.py`
#: documents why this is "agent" and not "ai": the definition schema calls it
#: "ai", the engine calls it "agent", and a stage stored under either spelling
#: has to land on the same step kind.
_STEP_KIND_BY_ENGINE_WORD = {"agent": "ai", "ai": "ai", "code": "code", "gate": "gate"}

#: What the importer will not guess. A dynamic stage missing any of these for
#: its kind is reported and its agent is left unpublished, rather than filled
#: in with something reasonable-looking.
_REQUIRED_BY_KIND = {
    "ai": ("a prompt body", "a non-empty output schema"),
    "code": ("code", "a language"),
    "gate": ("a gate kind",),
}


@dataclass
class AgentOutcome:
    """What happened to one agent, in words a person can act on."""

    slug: str
    source_registry: str
    stage_source: str
    #: "imported" | "unchanged" | "row_only" | "refused"
    result: str
    version: int | None = None
    prompts_imported: int = 0
    custom_agent_keys: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    outcomes: list[AgentOutcome] = field(default_factory=list)

    def add(self, outcome: AgentOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def refused(self) -> list[AgentOutcome]:
        return [o for o in self.outcomes if o.result == "refused"]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.result] = counts.get(outcome.result, 0) + 1
        return counts


class RegistryImporter:
    """Reads the registries; writes only ``config.*``."""

    def __init__(
        self,
        config_db: ConfigDatabase,
        firestore: FirestoreDB,
        configuration: ConfigurationService,
        prompts: UnifiedPromptStore,
    ) -> None:
        self._db = config_db
        self._fs = firestore
        self._config = configuration
        self._prompts = prompts

    async def run(self, *, actor: str, dry_run: bool = False) -> ImportReport:
        report = ImportReport()

        documents = [
            snapshot_to_dict(snapshot)
            async for snapshot in self._fs.collection(AGENTS).stream()
        ]
        dynamic = await self._dynamic_definitions()

        for document in sorted(documents, key=lambda d: str(d.get("slug") or d.get("id"))):
            slug = str(document.get("slug") or document.get("id") or "")
            if not slug:
                # A document with no slug cannot be addressed by anything
                # downstream, so importing it would create an unreachable row.
                report.add(
                    AgentOutcome(
                        slug=f"<no slug: {document.get('id')}>",
                        source_registry="middleware_agents",
                        stage_source="config",
                        result="refused",
                        reasons=["the document has neither a `slug` nor an id"],
                    )
                )
                continue
            if document.get("deleted_at") is not None:
                continue

            try:
                report.add(
                    await self._import_agent(
                        document, dynamic.get(slug), actor=actor, dry_run=dry_run
                    )
                )
            except ValidationRefusedError as refused:
                # Every reason at once, for the same argument the exception
                # itself makes: an author fixing an imported spec one refusal
                # per run learns about problem two only after fixing problem
                # one.
                logger.warning("import of %s did not validate", slug)
                report.add(
                    AgentOutcome(
                        slug=slug,
                        source_registry="middleware_agents",
                        stage_source="config",
                        result="refused",
                        reasons=[
                            f"{problem.get('code')}: {problem.get('message')}"
                            for problem in refused.problems
                        ],
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # One agent's problem must not abandon the other fourteen, and
                # it must not be swallowed either -- the CLI exits non-zero on
                # any refusal.
                logger.warning("import of %s failed", slug, exc_info=True)
                report.add(
                    AgentOutcome(
                        slug=slug,
                        source_registry="middleware_agents",
                        stage_source="config",
                        result="refused",
                        reasons=[f"{type(exc).__name__}: {exc}"],
                    )
                )
        return report

    # --- Reading ----------------------------------------------------------

    async def _dynamic_definitions(self) -> dict[str, dict[str, Any]]:
        """The dynamic-agent specs, keyed by the slug they configure.

        Both stores are read; ``agentDefinitions`` wins where they overlap,
        because it is the one the engine executes. A missing collection is not
        an error: neither store exists in every environment, and an importer
        that refused to run without them would be unusable in the one
        environment that most needs it.
        """

        found: dict[str, dict[str, Any]] = {}
        for collection in (DYNAMIC_AGENT_SPECS, AGENT_DEFINITIONS):
            try:
                rows = [
                    snapshot_to_dict(snapshot)
                    async for snapshot in self._fs.collection(collection).stream()
                ]
            except Exception:  # noqa: BLE001
                logger.info("%s is not readable here; skipping it", collection)
                continue
            for row in rows:
                slug = row.get("agentSlug") or row.get("slug") or row.get("id")
                if slug:
                    found[str(slug)] = {**row, "_registry": collection}
        return found

    # --- Writing ----------------------------------------------------------

    async def _import_agent(
        self,
        document: dict[str, Any],
        definition: dict[str, Any] | None,
        *,
        actor: str,
        dry_run: bool,
    ) -> AgentOutcome:
        slug = str(document.get("slug") or document["id"])
        # The vocabulary `agents_source_registry_vocabulary` allows. Not the
        # literal collection name for this service's own store: "agents" is
        # ambiguous across three repositories, and provenance that can be read
        # two ways is not provenance.
        registry = str(definition["_registry"]) if definition else "middleware_agents"

        # A stage list is data only when a dynamic definition supplies one.
        # Absent that, it is the display mirror of a compiled workflow.
        stages = list(definition.get("stages") or []) if definition else []
        stage_source = "config" if stages else "engine_code"

        outcome = AgentOutcome(
            slug=slug,
            source_registry=registry,
            stage_source=stage_source,
            result="row_only",
        )

        if dry_run:
            outcome.custom_agent_keys = len(document.get("custom_agent_keys") or [])
            if stages:
                problems = _stage_problems(stages)
                if problems:
                    outcome.result = "refused"
                    outcome.reasons = problems
                else:
                    outcome.result = "imported"
            return outcome

        await self._upsert_agent(document, slug, stage_source, registry, actor=actor)
        outcome.custom_agent_keys = await self._upsert_keys(document, slug)

        if not stages:
            # Prompts are still configuration for these, and importing them is
            # what makes a skillRef editable rather than only readable.
            outcome.prompts_imported = await self._import_stage_prompts(document)
            return outcome

        problems = _stage_problems(stages)
        if problems:
            outcome.result = "refused"
            outcome.reasons = problems
            return outcome

        version = await self._publish_version(slug, stages, actor=actor)
        if version is None:
            outcome.result = "unchanged"
        else:
            outcome.result = "imported"
            outcome.version = version
        return outcome

    async def _upsert_agent(
        self,
        document: dict[str, Any],
        slug: str,
        stage_source: str,
        registry: str,
        *,
        actor: str,
    ) -> None:
        """One row, with its provenance recorded.

        ``on conflict`` updates the descriptor fields and leaves
        ``published_version_id`` alone: the pointer is moved by a publish or a
        rollback and by nothing else, and an import that reset it would roll
        every agent back to nothing on a re-run.
        """

        await self._db.execute(
            """
            insert into agents (
                slug, name, description, agent_class_code, category, tags,
                credit_cost, is_public, status, capabilities, platforms,
                consumes_media, supports_target_date, stage_source,
                source_registry, source_id, imported_at, created_by, updated_by
            ) values (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, now(), $17, $17
            )
            on conflict (slug) do update set
                name = excluded.name,
                description = excluded.description,
                category = excluded.category,
                tags = excluded.tags,
                credit_cost = excluded.credit_cost,
                is_public = excluded.is_public,
                status = excluded.status,
                capabilities = excluded.capabilities,
                platforms = excluded.platforms,
                consumes_media = excluded.consumes_media,
                supports_target_date = excluded.supports_target_date,
                stage_source = excluded.stage_source,
                source_registry = excluded.source_registry,
                source_id = excluded.source_id,
                imported_at = now(),
                updated_by = excluded.updated_by
            """,
            slug,
            str(document.get("name") or slug),
            str(document.get("description") or ""),
            _agent_class_for(document),
            document.get("category"),
            list(document.get("tags") or []),
            int(document.get("credit_cost") or 0),
            bool(document.get("is_public", True)),
            str(document.get("status") or "enabled"),
            list(document.get("capabilities") or []),
            list(document.get("platforms") or []),
            bool(document.get("consumes_media", False)),
            bool(document.get("supports_target_date", False)),
            stage_source,
            registry,
            str(document.get("id") or slug),
            actor,
        )

    async def _upsert_keys(self, document: dict[str, Any], slug: str) -> int:
        """The portal keys that route to this agent.

        C4 §5 requires each key to appear exactly once across all agents, which
        the primary key enforces. A key already claimed by a DIFFERENT agent is
        a conflict and is raised, not reassigned: silently repointing it would
        move live portal traffic from one agent to another during an import.
        """

        keys = [str(k) for k in (document.get("custom_agent_keys") or []) if k]
        for key in keys:
            owner = await self._db.fetchval(
                "select agent_slug from agent_custom_agent_keys where custom_agent_key = $1",
                key,
            )
            if owner is not None and str(owner) != slug:
                raise ResourceConflictError(
                    f"portal key '{key}' is already claimed by agent '{owner}', and "
                    f"'{slug}' also declares it. C4 §5 requires exactly one owner; "
                    "an import will not silently move portal traffic between agents."
                )
            if owner is None:
                await self._db.execute(
                    "insert into agent_custom_agent_keys (custom_agent_key, agent_slug) "
                    "values ($1, $2)",
                    key,
                    slug,
                )
        return len(keys)

    async def _import_stage_prompts(self, document: dict[str, Any]) -> int:
        """Record the body behind every ``skillRef`` this agent's stages name.

        Delegated to S7's store, which reads ``promptVersions/{id}@{v}`` and
        records it append-only. Idempotent there, so a re-run imports nothing.
        A stage whose skillRef points at a document that does not exist is
        skipped and logged: the stage list is generated from engine source, so
        a dangling skillRef is a fact about the engine rather than something
        this import can fix.
        """

        imported = 0
        for stage in document.get("stages") or []:
            skill_ref = stage.get("skill_ref")
            if not skill_ref or "@" not in str(skill_ref):
                continue
            prompt_id, _, engine_version = str(skill_ref).partition("@")
            try:
                await self._prompts.versions(prompt_id, engine_version)
                imported += 1
            except Exception:  # noqa: BLE001
                logger.info(
                    "skillRef %s names a prompt document that is not readable; skipped",
                    skill_ref,
                )
        return imported

    async def _publish_version(
        self, slug: str, stages: list[dict[str, Any]], *, actor: str
    ) -> int | None:
        """Create, fill and publish one version -- unless it would be identical.

        The identity check is what makes a re-run a no-op instead of version
        four of the same thing. Compared on the step list as written, not on
        the whole row: `imported_at` moves on every run and would make every
        version look different from the last.
        """

        prompts = {
            str(stage.get("id") or stage.get("step_id")): await self._ensure_inline_prompt(
                slug, stage, actor=actor
            )
            for stage in stages
            if _STEP_KIND_BY_ENGINE_WORD.get(str(stage.get("kind") or "code")) == "ai"
        }
        steps = [_step_for(stage, prompts) for stage in stages]
        if await self._matches_published(slug, steps):
            return None

        draft = await self._config.create_version(
            slug,
            VersionCreate(
                defaults=VersionDefaults(),
                notes="Imported from the dynamic-agent registry (S5).",
            ),
            actor=actor,
        )
        await self._config.replace_steps(
            slug, draft.version, StepsReplace(steps=steps), actor=actor
        )
        # A publish that does not validate raises `ValidationRefusedError`
        # carrying every reason, and `run` records those against this agent.
        # Deliberately not caught here: the draft is left in place, so the
        # problems can be read off the version rather than only off a log.
        await self._config.publish(slug, draft.version, _publish_request(), actor=actor)
        return draft.version

    async def _ensure_inline_prompt(
        self, slug: str, stage: dict[str, Any], *, actor: str
    ) -> tuple[str, int]:
        """A dynamic stage's inline ``systemPrompt``, as a row in the one store.

        This is source number six in S7's list of six places a prompt lives,
        and the only one with no document of its own -- it is a field on a spec.
        So it is inserted here directly rather than through
        :class:`UnifiedPromptStore`, whose ``save`` also PROJECTS into
        ``promptVersions/{id}@{v}``: there is no engine prompt document for an
        inline prompt to project into, and writing one would invent an
        interface rather than record a fact.

        Deduped on the content hash, not on the key. A re-run of the import
        with the spec unchanged must not append a second identical version --
        `prompt_versions` is append-only by trigger, so a careless importer
        grows the history by one row per run forever and the only way back is
        a restore from backup.
        """

        step_id = str(stage.get("id") or stage.get("step_id"))
        body = _prompt_body(stage) or ""
        key = f"{slug}/{step_id}"
        digest = _content_hash(body)

        prompt_id = await self._db.fetchval(
            """
            insert into prompts (
                prompt_key, agent_slug, purpose, description,
                source_registry, source_id, imported_at, created_by
            ) values ($1, $2, 'skill', $3, 'dynamicAgentSpecs', $4, now(), $5)
            on conflict (prompt_key) do update set updated_at = now()
            returning id
            """,
            key,
            slug,
            f"Inline prompt of dynamic stage {step_id}",
            step_id,
            actor,
        )

        existing = await self._db.fetchval(
            "select version from prompt_versions where prompt_id = $1 and content_hash = $2 "
            "order by version limit 1",
            prompt_id,
            digest,
        )
        if existing is not None:
            return key, int(existing)

        version = await self._db.fetchval(
            "select coalesce(max(version), 0) + 1 from prompt_versions where prompt_id = $1",
            prompt_id,
        )
        version_id = await self._db.fetchval(
            """
            insert into prompt_versions (
                prompt_id, version, content, content_hash, notes, created_by
            ) values ($1, $2, $3, $4, $5, $6)
            returning id
            """,
            prompt_id,
            int(version),
            body,
            digest,
            "Imported from the dynamic-agent spec's inline systemPrompt (S5).",
            actor,
        )
        await self._db.execute(
            "update prompts set active_version_id = $1, updated_at = now() where id = $2",
            version_id,
            prompt_id,
        )
        return key, int(version)

    async def _matches_published(self, slug: str, steps: list[VersionStepWrite]) -> bool:
        published_id = await self._db.fetchval(
            "select published_version_id from agents where slug = $1", slug
        )
        if published_id is None:
            return False
        current = await self._db.fetch(
            "select step_id, kind_code, description, code, language, gate_kind, "
            "skill_ref, model_id "
            "from agent_version_steps where version_id = $1 order by position",
            published_id,
        )
        if len(current) != len(steps):
            return False
        return all(
            row["step_id"] == step.step_id
            and row["kind_code"] == step.kind
            and (row["description"] or "") == step.description
            and (row["code"] or None) == step.code
            and (row["language"] or None) == step.language
            and (row["gate_kind"] or None) == step.gate_kind
            and (row["skill_ref"] or None) == step.skill_ref
            and (row["model_id"] or None) == step.model_id
            for row, step in zip(current, steps, strict=True)
        )


# --- Stage translation ------------------------------------------------------


def _publish_request() -> Any:
    from app.api.schemas.configuration import PublishRequest

    return PublishRequest(note="S5 import")


def _agent_class_for(document: dict[str, Any]) -> str:
    """The agent class, from the document's own field or its capabilities.

    ``agent_class_code`` is a FK, so a wrong value is a refused insert rather
    than a bad row -- which is why guessing here is safe in a way guessing a
    prompt is not.
    """

    declared = document.get("agent_class_code") or document.get("agent_class")
    if declared:
        return str(declared)
    capabilities = [str(c) for c in (document.get("capabilities") or [])]
    for capability in capabilities:
        if capability.startswith("draft_"):
            return "drafting"
        if capability.startswith("run_"):
            return "research"
        if capability.startswith("produce_") or capability.startswith("build_"):
            return "production"
        if capability == "orchestrate_campaign":
            return "orchestration"
        if capability == "run_setup":
            return "setup"
    return "drafting"


def _stage_problems(stages: list[dict[str, Any]]) -> list[str]:
    """Everything about these stages the importer will not invent.

    Reported all at once rather than on the first miss: an author fixing a
    dynamic spec wants the list, not one item per run.
    """

    problems: list[str] = []
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        step_id = str(stage.get("id") or stage.get("step_id") or "")
        where = step_id or f"stage {index}"
        if not step_id:
            problems.append(f"{where}: no step id")
            continue
        if step_id in seen:
            problems.append(f"{where}: appears twice; position is the order and cannot repeat")
        seen.add(step_id)

        kind = _STEP_KIND_BY_ENGINE_WORD.get(str(stage.get("kind") or "code"))
        if kind is None:
            problems.append(f"{where}: unknown stage kind {stage.get('kind')!r}")
            continue

        if kind == "ai":
            if not _prompt_body(stage):
                problems.append(f"{where}: an ai stage needs a prompt body")
            if not _output_schema(stage):
                problems.append(f"{where}: an ai stage needs a non-empty output schema")
        elif kind == "code":
            if not stage.get("code"):
                problems.append(f"{where}: a code stage needs code")
            if not stage.get("language"):
                problems.append(f"{where}: a code stage needs a language")
        elif kind == "gate" and not stage.get("gate_kind") and not stage.get("gateKind"):
            problems.append(f"{where}: a gate needs a gate kind")
    return problems


def _prompt_body(stage: dict[str, Any]) -> str | None:
    body = stage.get("systemPrompt") or stage.get("system_prompt") or stage.get("prompt")
    return str(body) if body else None


def _output_schema(stage: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stage.get("outputSchema") or stage.get("output_schema") or []
    return [f for f in raw if isinstance(f, dict) and f.get("name")]


def _step_for(
    stage: dict[str, Any], prompts: dict[str, tuple[str, int]]
) -> VersionStepWrite:
    """One dynamic stage as a step this schema can hold.

    Only called after `_stage_problems` has returned nothing, so every field
    the kind requires is known to be present. Written as two passes rather than
    one for exactly that reason: the checking pass reports the whole list, and
    this pass does not have to decide what to do about a gap.
    """

    kind = _STEP_KIND_BY_ENGINE_WORD[str(stage.get("kind") or "code")]
    schema = [
        OutputField(
            name=str(f["name"]),
            type=str(f.get("type") or "string"),  # type: ignore[arg-type]
            description=f.get("description"),
            optional=bool(f.get("optional", False)),
        )
        for f in _output_schema(stage)
    ]
    step_id = str(stage.get("id") or stage.get("step_id"))
    prompt_key, prompt_version = prompts.get(step_id, (None, None))
    return VersionStepWrite(
        step_id=step_id,
        kind=kind,  # type: ignore[arg-type]
        description=str(stage.get("label") or stage.get("description") or "")[:2000],
        prompt_key=prompt_key,
        prompt_version=prompt_version,
        model_id=stage.get("model_id") or stage.get("modelId"),
        output_schema=schema or None,
        language=stage.get("language") if kind == "code" else None,
        code=stage.get("code") if kind == "code" else None,
        gate_kind=(stage.get("gate_kind") or stage.get("gateKind")) if kind == "gate" else None,
        skill_ref=stage.get("skill_ref") or stage.get("skillRef"),
        allowed_tools=[str(t) for t in (stage.get("allowed_tools") or stage.get("tools") or [])],
        config={"imported_from": "dynamic_agent_registry"},
    )
