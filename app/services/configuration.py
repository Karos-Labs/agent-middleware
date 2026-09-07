"""The Configuration API — versions, the validating publish, rollback and diff.

An agent version is authored as a draft, validated as a whole, and frozen. The
freeze is what makes a run reproducible (C6), so everything here exists to make
sure that what gets frozen is coherent.

## Publish is where the work is

SCRUM-218 lists the checks, and each one exists because of a specific way a
version can be broken while looking fine:

* **A schema for every step.** The engine builds a Zod shape from
  ``output_schema``; a field named ``2nd-draft`` is not an identifier and the
  step fails at run time rather than at publish.
* **Every reference resolved.** A prompt version, a model or a tool that does
  not exist is a run that dies on the step that needs it -- twenty minutes in,
  with the earlier steps already billed.
* **An acyclic graph, and every ``{{steps.*}}`` pointing upward.** Stages run
  in order, each one's output feeding the next. A reference to a *later* step
  is not a cycle the engine detects; it resolves to nothing, and the model is
  handed a prompt with an empty hole in it. The output is plausible and wrong,
  which is the failure mode this whole ticket is about.
* **The referenced FIELD exists.** ``{{steps.10-draft.headline}}`` where that
  step's schema has no ``headline`` is the same silent hole, one level down.
* **The model exists and has a pricing row.** S12's finding: a model with no
  price is costed at Sonnet's $3/$15 by every path in the platform, silently.
* **Every tool permitted for the agent_class.** ``capability_policy`` is
  default-deny, so a tool nobody has granted is refused rather than quietly
  available.

And then, in ONE transaction: freeze the version, move the pointer, write the
audit row. Not two transactions -- a frozen version nothing points at is a
version that cannot be edited and is not live, which is the one state with no
way out.

## Rollback moves a pointer

No data change and no deletion, which is only true because the stored status is
``draft``/``frozen`` and "which version is live" is
``agents.published_version_id`` alone (see migrations/README.md). Rolling back
writes one uuid and one audit row.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

import asyncpg

from app.api.schemas.configuration import (
    FieldChange,
    OutputField,
    PublishRequest,
    PublishResult,
    RollbackRequest,
    SelfCritique,
    StepBounds,
    StepChange,
    StepsReplace,
    ValidationProblem,
    VersionCreate,
    VersionDefaults,
    VersionDiff,
    VersionRead,
    VersionStepRead,
    VersionStepWrite,
    VersionSummary,
    VersionUpdate,
)
from app.core.exceptions import (
    InvalidStateError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationRefusedError,
)
from app.db.postgres import ConfigDatabase

logger = logging.getLogger(__name__)

#: ``{{steps.<step-id>.<field>}}`` -- the engine's own reference syntax. The
#: field part is optional so that a reference to a step's whole output is
#: recognised rather than silently ignored by the validator.
STEP_REFERENCE = re.compile(
    r"\{\{\s*steps\.([a-z0-9][a-z0-9-]*)"
    r"(?:\.([A-Za-z_][A-Za-z0-9_]*))?[^}]*\}\}"
)

#: A prompt diff big enough to be unreadable is a prompt diff nobody reads.
MAX_PROMPT_DIFF_CHARS = 20_000


class ConfigurationService:
    """Reads and writes agent versions in the configuration schema."""

    def __init__(self, database: ConfigDatabase) -> None:
        self._db = database

    # =====================================================================
    # Reading
    # =====================================================================

    async def list_versions(self, agent_slug: str) -> list[VersionSummary]:
        await self._require_agent(agent_slug)
        rows = await self._db.fetch(
            """
            select v.id, v.agent_slug, v.version, v.status, s.lifecycle, v.notes,
                   v.frozen_at, v.frozen_by, v.created_at, v.created_by,
                   (select count(*) from agent_version_steps st where st.version_id = v.id)
                       as step_count
              from agent_versions v
              join agent_version_state s on s.id = v.id
             where v.agent_slug = $1
             order by v.version desc
            """,
            agent_slug,
        )
        return [VersionSummary(**{**dict(row), "id": str(row["id"])}) for row in rows]

    async def get_version(self, agent_slug: str, version: int) -> VersionRead:
        row = await self._require_version(agent_slug, version)
        steps = await self._read_steps(row["id"])
        return VersionRead(
            id=str(row["id"]),
            agent_slug=row["agent_slug"],
            version=row["version"],
            status=row["status"],
            lifecycle=row["lifecycle"],
            step_count=len(steps),
            notes=row["notes"],
            frozen_at=row["frozen_at"],
            frozen_by=row["frozen_by"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            defaults=VersionDefaults(
                default_model_id=row["default_model_id"],
                default_provider_policy=row["default_provider_policy"],
                dedupe_against_history=row["dedupe_against_history"],
                agent_step_timeout_ms=row["agent_step_timeout_ms"],
            ),
            steps=steps,
        )

    async def _read_steps(
        self, version_id: Any, connection: asyncpg.Connection | None = None
    ) -> list[VersionStepRead]:
        query = """
            select s.step_id, s.position, s.kind_code, s.description,
                   s.prompt_version_id, s.model_id, s.provider_policy,
                   s.fallback_model_id, s.output_schema, s.max_steps, s.max_tokens,
                   s.max_malformed_turns, s.self_critique_gate_tool,
                   s.self_critique_max_revisions, s.self_critique_args,
                   s.language, s.code, s.code_timeout_ms, s.is_gate, s.gate_kind,
                   s.skill_ref, s.config,
                   p.prompt_key, pv.version as prompt_version, pv.content_hash,
                   coalesce(
                       (select array_agg(tc.tool_code order by tc.tool_code)
                          from tool_config tc
                         where tc.scope = 'step' and tc.step_row_id = s.id),
                       '{}'::text[]
                   ) as allowed_tools
              from agent_version_steps s
              left join prompt_versions pv on pv.id = s.prompt_version_id
              left join prompts p on p.id = pv.prompt_id
             where s.version_id = $1
             order by s.position
        """
        rows = (
            await connection.fetch(query, version_id)
            if connection is not None
            else await self._db.fetch(query, version_id)
        )
        return [
            VersionStepRead(
                step_id=row["step_id"],
                position=row["position"],
                kind=row["kind_code"],
                description=row["description"],
                prompt_key=row["prompt_key"],
                prompt_version=row["prompt_version"],
                prompt_version_id=(
                    str(row["prompt_version_id"]) if row["prompt_version_id"] else None
                ),
                prompt_content_hash=row["content_hash"],
                model_id=row["model_id"],
                provider_policy=row["provider_policy"],
                fallback_model_id=row["fallback_model_id"],
                output_schema=(
                    [OutputField(**field) for field in row["output_schema"]]
                    if row["output_schema"]
                    else None
                ),
                bounds=StepBounds(
                    max_steps=row["max_steps"],
                    max_tokens=row["max_tokens"],
                    max_malformed_turns=row["max_malformed_turns"],
                ),
                self_critique=(
                    SelfCritique(
                        gate_tool=row["self_critique_gate_tool"],
                        max_revisions=row["self_critique_max_revisions"] or 1,
                        gate_args=row["self_critique_args"] or {},
                    )
                    if row["self_critique_gate_tool"]
                    else None
                ),
                language=row["language"],
                code=row["code"],
                code_timeout_ms=row["code_timeout_ms"],
                is_gate=row["is_gate"],
                gate_kind=row["gate_kind"],
                skill_ref=row["skill_ref"],
                allowed_tools=list(row["allowed_tools"] or []),
                config=row["config"] or {},
            )
            for row in rows
        ]

    # =====================================================================
    # Authoring
    # =====================================================================

    async def create_version(
        self, agent_slug: str, payload: VersionCreate, *, actor: str
    ) -> VersionRead:
        """A new draft, optionally cloned from an existing version.

        Cloning is the normal path: a version is almost always the previous one
        with something changed, and authoring forty steps again to change one
        of them is how a draft ends up subtly different in a way nobody
        intended.
        """

        await self._require_agent(agent_slug)

        async with self._db.transaction() as connection:
            source = None
            if payload.from_version is not None:
                source = await connection.fetchrow(
                    "select * from agent_versions where agent_slug = $1 and version = $2",
                    agent_slug,
                    payload.from_version,
                )
                if source is None:
                    raise ResourceNotFoundError(
                        f"version {payload.from_version} of agent", agent_slug
                    )

            defaults = payload.defaults
            if defaults is None and source is not None:
                defaults = VersionDefaults(
                    default_model_id=source["default_model_id"],
                    default_provider_policy=source["default_provider_policy"],
                    dedupe_against_history=source["dedupe_against_history"],
                    agent_step_timeout_ms=source["agent_step_timeout_ms"],
                )
            defaults = defaults or VersionDefaults()

            next_version = await connection.fetchval(
                "select coalesce(max(version), 0) + 1 from agent_versions where agent_slug = $1",
                agent_slug,
            )
            version_id = await connection.fetchval(
                """
                insert into agent_versions (
                    agent_slug, version, status, default_model_id,
                    default_provider_policy, dedupe_against_history,
                    agent_step_timeout_ms, notes, created_by
                ) values ($1, $2, 'draft', $3, $4, $5, $6, $7, $8)
                returning id
                """,
                agent_slug,
                next_version,
                defaults.default_model_id,
                defaults.default_provider_policy,
                defaults.dedupe_against_history,
                defaults.agent_step_timeout_ms,
                payload.notes,
                actor,
            )

            if source is not None:
                await self._clone_steps(connection, source["id"], version_id)

            await self._audit(
                connection,
                actor=actor,
                action="create",
                entity_type="agent_version",
                entity_id=str(version_id),
                agent_slug=agent_slug,
                after={
                    "version": next_version,
                    "cloned_from": payload.from_version,
                },
                note=payload.notes,
            )

        return await self.get_version(agent_slug, next_version)

    async def _clone_steps(
        self, connection: asyncpg.Connection, source_id: Any, target_id: Any
    ) -> None:
        """Copy a version's steps, and the tool grants that hang off them.

        The grants are the easy thing to forget: they live in `tool_config`
        keyed by step row, not on the step, so a clone that copies only the
        steps produces a version whose every AI stage has no tools -- which
        fails at run time on the first tool call, not at publish.
        """

        new_ids = await connection.fetch(
            """
            insert into agent_version_steps (
                version_id, step_id, position, kind_code, description,
                prompt_version_id, model_id, provider_policy, fallback_model_id,
                output_schema, max_steps, max_tokens, max_malformed_turns,
                self_critique_gate_tool, self_critique_max_revisions,
                self_critique_args, language, code, code_timeout_ms, is_gate,
                gate_kind, skill_ref, config
            )
            select $2, step_id, position, kind_code, description,
                   prompt_version_id, model_id, provider_policy, fallback_model_id,
                   output_schema, max_steps, max_tokens, max_malformed_turns,
                   self_critique_gate_tool, self_critique_max_revisions,
                   self_critique_args, language, code, code_timeout_ms, is_gate,
                   gate_kind, skill_ref, config
              from agent_version_steps
             where version_id = $1
             order by position
            returning id, step_id
            """,
            source_id,
            target_id,
        )
        by_step_id = {row["step_id"]: row["id"] for row in new_ids}

        grants = await connection.fetch(
            """
            select s.step_id, tc.tool_code, tc.config, tc.secret_ref
              from tool_config tc
              join agent_version_steps s on s.id = tc.step_row_id
             where tc.scope = 'step' and s.version_id = $1
            """,
            source_id,
        )
        for grant in grants:
            await connection.execute(
                """
                insert into tool_config (tool_code, scope, step_row_id, config, secret_ref)
                values ($1, 'step', $2, $3, $4)
                """,
                grant["tool_code"],
                by_step_id[grant["step_id"]],
                grant["config"],
                grant["secret_ref"],
            )

    async def update_version(
        self, agent_slug: str, version: int, payload: VersionUpdate, *, actor: str
    ) -> VersionRead:
        row = await self._require_version(agent_slug, version)
        self._require_draft(row)

        async with self._db.transaction() as connection:
            if payload.defaults is not None:
                await connection.execute(
                    """
                    update agent_versions
                       set default_model_id = $2, default_provider_policy = $3,
                           dedupe_against_history = $4, agent_step_timeout_ms = $5
                     where id = $1
                    """,
                    row["id"],
                    payload.defaults.default_model_id,
                    payload.defaults.default_provider_policy,
                    payload.defaults.dedupe_against_history,
                    payload.defaults.agent_step_timeout_ms,
                )
            if payload.notes is not None:
                await connection.execute(
                    "update agent_versions set notes = $2 where id = $1", row["id"], payload.notes
                )
            await self._audit(
                connection,
                actor=actor,
                action="update",
                entity_type="agent_version",
                entity_id=str(row["id"]),
                agent_slug=agent_slug,
                after=payload.model_dump(mode="json", exclude_unset=True),
            )
        return await self.get_version(agent_slug, version)

    async def delete_version(self, agent_slug: str, version: int, *, actor: str) -> None:
        """Only a draft. The database refuses the rest, and says why."""

        row = await self._require_version(agent_slug, version)
        self._require_draft(row)

        async with self._db.transaction() as connection:
            await self._audit(
                connection,
                actor=actor,
                action="delete",
                entity_type="agent_version",
                entity_id=str(row["id"]),
                agent_slug=agent_slug,
                before={"version": version},
            )
            await connection.execute("delete from agent_versions where id = $1", row["id"])

    async def replace_steps(
        self, agent_slug: str, version: int, payload: StepsReplace, *, actor: str
    ) -> VersionRead:
        """Replace a draft's whole step list, in one transaction.

        Whole-list rather than per-step, because order is the index and a
        partial update cannot express a reorder without a window in which two
        steps claim one position -- which the unique constraint would refuse
        half-way through, leaving the draft in neither shape.
        """

        row = await self._require_version(agent_slug, version)
        self._require_draft(row)

        async with self._db.transaction() as connection:
            resolved = [
                await self._resolve_step(connection, step) for step in payload.steps
            ]
            await connection.execute(
                "delete from agent_version_steps where version_id = $1", row["id"]
            )
            for position, (step, prompt_version_id) in enumerate(resolved):
                step_row_id = await connection.fetchval(
                    """
                    insert into agent_version_steps (
                        version_id, step_id, position, kind_code, description,
                        prompt_version_id, model_id, provider_policy,
                        fallback_model_id, output_schema, max_steps, max_tokens,
                        max_malformed_turns, self_critique_gate_tool,
                        self_critique_max_revisions, self_critique_args,
                        language, code, code_timeout_ms, is_gate, gate_kind,
                        skill_ref, config
                    ) values (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15, $16, $17, $18, $19, $20, $21, $22, $23
                    ) returning id
                    """,
                    row["id"],
                    step.step_id,
                    position,
                    step.kind,
                    step.description,
                    prompt_version_id,
                    step.model_id,
                    step.provider_policy,
                    step.fallback_model_id,
                    (
                        [field.model_dump(mode="json") for field in step.output_schema]
                        if step.output_schema is not None
                        else None
                    ),
                    step.bounds.max_steps,
                    step.bounds.max_tokens,
                    step.bounds.max_malformed_turns,
                    step.self_critique.gate_tool if step.self_critique else None,
                    step.self_critique.max_revisions if step.self_critique else None,
                    step.self_critique.gate_args if step.self_critique else None,
                    step.language,
                    step.code,
                    step.code_timeout_ms,
                    step.kind == "gate",
                    step.gate_kind,
                    step.skill_ref,
                    step.config,
                )
                for tool_code in dict.fromkeys(step.allowed_tools):
                    await connection.execute(
                        """
                        insert into tool_config (tool_code, scope, step_row_id, config)
                        values ($1, 'step', $2, '{}'::jsonb)
                        """,
                        tool_code,
                        step_row_id,
                    )

            await self._audit(
                connection,
                actor=actor,
                action="update",
                entity_type="agent_version_steps",
                entity_id=str(row["id"]),
                agent_slug=agent_slug,
                after={"step_ids": [step.step_id for step in payload.steps]},
            )

        return await self.get_version(agent_slug, version)

    async def _resolve_step(
        self, connection: asyncpg.Connection, step: VersionStepWrite
    ) -> tuple[VersionStepWrite, Any]:
        """Turn a prompt named by (key, version) into the id the row stores.

        Resolved at write time rather than at read: pinning the exact version
        is what makes a frozen version immune to a later prompt edit, and
        storing the key would defer the resolution to run time, which is the
        deferral this whole design removes.
        """

        if step.prompt_version_id is not None:
            exists = await connection.fetchval(
                "select id from prompt_versions where id = $1::uuid", step.prompt_version_id
            )
            if exists is None:
                raise ResourceNotFoundError("prompt version", step.prompt_version_id)
            return step, exists

        if step.prompt_key is None:
            return step, None

        prompt_version_id = await connection.fetchval(
            """
            select pv.id
              from prompt_versions pv
              join prompts p on p.id = pv.prompt_id
             where p.prompt_key = $1 and pv.version = $2
            """,
            step.prompt_key,
            step.prompt_version,
        )
        if prompt_version_id is None:
            raise ResourceNotFoundError(
                "prompt version", f"{step.prompt_key}@{step.prompt_version}"
            )
        return step, prompt_version_id

    # =====================================================================
    # Publish
    # =====================================================================

    async def publish(
        self, agent_slug: str, version: int, payload: PublishRequest, *, actor: str
    ) -> PublishResult:
        """Validate the whole version, then freeze it and move the pointer.

        One transaction from the row lock to the audit row. The lock matters:
        two publishes of the same agent racing would otherwise both validate
        against the same state and the second would overwrite the first's
        pointer with an older version.
        """

        async with self._db.transaction() as connection:
            row = await connection.fetchrow(
                """
                select v.*, a.published_version_id, a.agent_class_code
                  from agent_versions v
                  join agents a on a.slug = v.agent_slug
                 where v.agent_slug = $1 and v.version = $2
                 for update of v
                """,
                agent_slug,
                version,
            )
            if row is None:
                raise ResourceNotFoundError(f"version {version} of agent", agent_slug)
            if row["status"] != "draft":
                raise InvalidStateError(
                    f"version {version} of '{agent_slug}' is already frozen; publishing it "
                    "again would change nothing. To make it live again, roll back to it."
                )

            problems = await self._validate(connection, row)
            if problems:
                if payload.dry_run:
                    return PublishResult(
                        agent_slug=agent_slug,
                        version=version,
                        version_id=str(row["id"]),
                        previous_version_id=(
                            str(row["published_version_id"])
                            if row["published_version_id"]
                            else None
                        ),
                        previous_version=await self._version_number_of(
                            connection, row["published_version_id"]
                        ),
                        published_at=row["created_at"],
                        published_by=actor,
                        dry_run=True,
                        problems=problems,
                    )
                raise ValidationRefusedError(
                    f"version {version} of '{agent_slug}' cannot be published: "
                    f"{len(problems)} problem(s)",
                    [problem.model_dump(mode="json") for problem in problems],
                )

            previous_id = row["published_version_id"]
            previous_number = await self._version_number_of(connection, previous_id)

            if payload.dry_run:
                return PublishResult(
                    agent_slug=agent_slug,
                    version=version,
                    version_id=str(row["id"]),
                    previous_version_id=str(previous_id) if previous_id else None,
                    previous_version=previous_number,
                    published_at=row["created_at"],
                    published_by=actor,
                    dry_run=True,
                )

            # Freeze BEFORE moving the pointer: the database refuses a pointer
            # to a draft, so the other order fails on its own guard.
            frozen_at = await connection.fetchval(
                """
                update agent_versions
                   set status = 'frozen', frozen_at = now(), frozen_by = $2
                 where id = $1
                returning frozen_at
                """,
                row["id"],
                actor,
            )
            await connection.execute(
                "update agents set published_version_id = $2, updated_by = $3 where slug = $1",
                agent_slug,
                row["id"],
                actor,
            )
            audit_id = await self._audit(
                connection,
                actor=actor,
                action="publish",
                entity_type="agent_version",
                entity_id=str(row["id"]),
                agent_slug=agent_slug,
                before={
                    "published_version_id": str(previous_id) if previous_id else None,
                    "published_version": previous_number,
                },
                after={"published_version_id": str(row["id"]), "published_version": version},
                note=payload.note,
            )

        logger.info(
            "published version %s of %s (was %s)",
            version,
            agent_slug,
            previous_number,
            extra={"agent_slug": agent_slug, "version": version, "actor": actor},
        )
        return PublishResult(
            agent_slug=agent_slug,
            version=version,
            version_id=str(row["id"]),
            previous_version_id=str(previous_id) if previous_id else None,
            previous_version=previous_number,
            published_at=frozen_at,
            published_by=actor,
            audit_id=audit_id,
        )

    async def _validate(
        self, connection: asyncpg.Connection, version_row: asyncpg.Record
    ) -> list[ValidationProblem]:
        """Every reason this version cannot be published. All of them, at once."""

        problems: list[ValidationProblem] = []
        steps = await connection.fetch(
            """
            select s.id, s.step_id, s.position, s.kind_code, s.output_schema,
                   s.prompt_version_id, s.model_id, s.fallback_model_id,
                   s.provider_policy, s.self_critique_gate_tool,
                   pv.content as prompt_content
              from agent_version_steps s
              left join prompt_versions pv on pv.id = s.prompt_version_id
             where s.version_id = $1
             order by s.position
            """,
            version_row["id"],
        )

        if not steps:
            problems.append(
                ValidationProblem(
                    code="no_steps",
                    message="a version with no steps would dispatch a run that does nothing",
                )
            )
            return problems

        position_of = {row["step_id"]: row["position"] for row in steps}
        fields_of = {
            row["step_id"]: {
                field["name"] for field in (row["output_schema"] or [])
            }
            for row in steps
        }

        for row in steps:
            step_id = row["step_id"]
            problems.extend(self._check_output_schema(row))
            problems.extend(self._check_references(row, position_of, fields_of))
            problems.extend(
                await self._check_models(connection, row, version_row, step_id)
            )
            problems.extend(await self._check_tools(connection, row, version_row))

        return problems

    def _check_output_schema(self, row: asyncpg.Record) -> list[ValidationProblem]:
        """The engine builds a Zod shape from this; a bad field fails at run time."""

        problems: list[ValidationProblem] = []
        schema = row["output_schema"]
        if row["kind_code"] != "ai":
            return problems
        if not schema:
            # The database's own kind guard catches this on insert; repeated
            # here because a version can be published long after it was
            # authored, and a schema that disagrees with the guard is worth
            # catching in both places rather than trusting one.
            problems.append(
                ValidationProblem(
                    code="missing_output_schema",
                    message="an ai step must declare what it returns",
                    step_id=row["step_id"],
                )
            )
            return problems

        seen: set[str] = set()
        for field in schema:
            try:
                OutputField(**field)
            except Exception as exc:  # pragma: no cover - message varies by pydantic
                problems.append(
                    ValidationProblem(
                        code="invalid_output_field",
                        message=f"{field.get('name', '?')}: {exc}",
                        step_id=row["step_id"],
                        field=str(field.get("name")),
                    )
                )
                continue
            if field["name"] in seen:
                problems.append(
                    ValidationProblem(
                        code="duplicate_output_field",
                        message=f"'{field['name']}' is declared twice",
                        step_id=row["step_id"],
                        field=field["name"],
                    )
                )
            seen.add(field["name"])
        return problems

    def _check_references(
        self,
        row: asyncpg.Record,
        position_of: dict[str, int],
        fields_of: dict[str, set[str]],
    ) -> list[ValidationProblem]:
        """`{{steps.X.y}}` must name an EARLIER step and a field it declares.

        A reference to a later step is not a cycle the engine detects. Stages
        run in array order, so it resolves to nothing and the model is handed a
        prompt with a hole in it -- and produces something plausible. Same for a
        field the referenced step does not return.
        """

        problems: list[ValidationProblem] = []
        content = row["prompt_content"] or ""
        for match in STEP_REFERENCE.finditer(content):
            target, field = match.group(1), match.group(2)

            if target not in position_of:
                problems.append(
                    ValidationProblem(
                        code="unknown_step_reference",
                        message=f"references step '{target}', which is not in this version",
                        step_id=row["step_id"],
                        field=field,
                    )
                )
                continue

            if target == row["step_id"]:
                problems.append(
                    ValidationProblem(
                        code="self_reference",
                        message="references its own output, which does not exist yet",
                        step_id=row["step_id"],
                        field=field,
                    )
                )
                continue

            if position_of[target] >= row["position"]:
                problems.append(
                    ValidationProblem(
                        code="forward_step_reference",
                        message=(
                            f"references step '{target}', which runs later "
                            f"(position {position_of[target]} vs {row['position']}). "
                            "Stages run in order, so this resolves to nothing and the "
                            "model is handed a prompt with a hole in it."
                        ),
                        step_id=row["step_id"],
                        field=field,
                    )
                )
                continue

            if field is not None and field not in fields_of.get(target, set()):
                declared = ", ".join(sorted(fields_of.get(target, set()))) or "nothing"
                problems.append(
                    ValidationProblem(
                        code="unknown_step_field",
                        message=(
                            f"references '{target}.{field}', but that step declares "
                            f"{declared}"
                        ),
                        step_id=row["step_id"],
                        field=field,
                    )
                )
        return problems

    async def _check_models(
        self,
        connection: asyncpg.Connection,
        row: asyncpg.Record,
        version_row: asyncpg.Record,
        step_id: str,
    ) -> list[ValidationProblem]:
        """The model exists AND has a price (S12).

        A priceless model is costed at Sonnet's $3/$15 by every path in the
        platform, silently -- so publishing a version that names one produces
        a cost report that is wrong and looks fine.
        """

        problems: list[ValidationProblem] = []
        candidates = [
            (row["model_id"] or version_row["default_model_id"], "model_id"),
            (row["fallback_model_id"], "fallback_model_id"),
        ]
        for model_id, field in candidates:
            if not model_id:
                if field == "model_id" and row["kind_code"] == "ai":
                    problems.append(
                        ValidationProblem(
                            code="no_model",
                            message=(
                                "no model on the step and none on the version's defaults"
                            ),
                            step_id=step_id,
                            field=field,
                        )
                    )
                continue

            model = await connection.fetchrow(
                "select model_id, availability, input_per_1m, output_per_1m "
                "from models where model_id = $1",
                model_id,
            )
            if model is None:
                problems.append(
                    ValidationProblem(
                        code="unknown_model",
                        message=f"'{model_id}' is not in the catalog",
                        step_id=step_id,
                        field=field,
                    )
                )
                continue
            if model["input_per_1m"] is None or model["output_per_1m"] is None:
                problems.append(
                    ValidationProblem(
                        code="unpriced_model",
                        message=(
                            f"'{model_id}' has no price, so every step on it would be "
                            "costed at the fallback rate"
                        ),
                        step_id=step_id,
                        field=field,
                    )
                )
            if model["availability"] == "retired":
                problems.append(
                    ValidationProblem(
                        code="retired_model",
                        message=f"'{model_id}' is retired and cannot be selected",
                        step_id=step_id,
                        field=field,
                    )
                )
        return problems

    async def _check_tools(
        self,
        connection: asyncpg.Connection,
        row: asyncpg.Record,
        version_row: asyncpg.Record,
    ) -> list[ValidationProblem]:
        """Every tool the step may call must be granted to the agent's class.

        `capability_policy` is default-deny, so the absence of a row is a no.
        That direction is the value: a tool nobody has thought about is not
        silently available to an agent that should not have it.
        """

        problems: list[ValidationProblem] = []
        grants = await connection.fetch(
            """
            select tc.tool_code,
                   t.code is not null as tool_exists,
                   t.status as tool_status,
                   exists (
                       select 1 from capability_policy p
                        where p.agent_class_code = $2
                          and p.subject_type = 'tool'
                          and p.subject = tc.tool_code
                          and p.decision = 'allow'
                   ) as permitted
              from tool_config tc
              left join tools t on t.code = tc.tool_code
             where tc.scope = 'step' and tc.step_row_id = $1
            """,
            row["id"],
            version_row["agent_class_code"],
        )
        for grant in grants:
            if not grant["tool_exists"]:
                problems.append(
                    ValidationProblem(
                        code="unknown_tool",
                        message=f"'{grant['tool_code']}' is not a registered tool",
                        step_id=row["step_id"],
                    )
                )
                continue
            if grant["tool_status"] == "retired":
                problems.append(
                    ValidationProblem(
                        code="retired_tool",
                        message=f"'{grant['tool_code']}' is retired",
                        step_id=row["step_id"],
                    )
                )
            if not grant["permitted"]:
                problems.append(
                    ValidationProblem(
                        code="tool_not_permitted",
                        message=(
                            f"'{grant['tool_code']}' is not granted to agent class "
                            f"'{version_row['agent_class_code']}'"
                        ),
                        step_id=row["step_id"],
                    )
                )

        if row["self_critique_gate_tool"]:
            exists = await connection.fetchval(
                "select 1 from tools where code = $1", row["self_critique_gate_tool"]
            )
            if not exists:
                problems.append(
                    ValidationProblem(
                        code="unknown_gate_tool",
                        message=(
                            f"self-critique names '{row['self_critique_gate_tool']}', "
                            "which is not a registered tool"
                        ),
                        step_id=row["step_id"],
                        field="self_critique.gate_tool",
                    )
                )
        return problems

    # =====================================================================
    # Rollback
    # =====================================================================

    async def rollback(
        self, agent_slug: str, payload: RollbackRequest, *, actor: str
    ) -> PublishResult:
        """Point the agent at an earlier frozen version. No data change.

        Not a re-publish: the target is already frozen and already validated,
        and re-validating it could refuse a rollback because a model has since
        been retired -- which is exactly when a rollback is most needed.
        """

        async with self._db.transaction() as connection:
            agent = await connection.fetchrow(
                "select slug, published_version_id from agents where slug = $1 for update",
                agent_slug,
            )
            if agent is None:
                raise ResourceNotFoundError("agent", agent_slug)

            target = await connection.fetchrow(
                "select id, version, status from agent_versions "
                "where agent_slug = $1 and version = $2",
                agent_slug,
                payload.to_version,
            )
            if target is None:
                raise ResourceNotFoundError(
                    f"version {payload.to_version} of agent", agent_slug
                )
            if target["status"] != "frozen":
                raise InvalidStateError(
                    f"version {payload.to_version} of '{agent_slug}' is a draft; only a "
                    "frozen version has ever been live, and a draft is still editable"
                )
            if agent["published_version_id"] == target["id"]:
                raise ResourceConflictError(
                    f"'{agent_slug}' already runs version {payload.to_version}"
                )

            previous_id = agent["published_version_id"]
            previous_number = await self._version_number_of(connection, previous_id)

            await connection.execute(
                "update agents set published_version_id = $2, updated_by = $3 where slug = $1",
                agent_slug,
                target["id"],
                actor,
            )
            audit_id = await self._audit(
                connection,
                actor=actor,
                action="rollback",
                entity_type="agent_version",
                entity_id=str(target["id"]),
                agent_slug=agent_slug,
                before={
                    "published_version_id": str(previous_id) if previous_id else None,
                    "published_version": previous_number,
                },
                after={
                    "published_version_id": str(target["id"]),
                    "published_version": target["version"],
                },
                note=payload.reason,
            )
            published_at = await connection.fetchval(
                "select frozen_at from agent_versions where id = $1", target["id"]
            )

        logger.info(
            "rolled %s back from version %s to %s",
            agent_slug,
            previous_number,
            target["version"],
            extra={"agent_slug": agent_slug, "actor": actor},
        )
        return PublishResult(
            agent_slug=agent_slug,
            version=target["version"],
            version_id=str(target["id"]),
            previous_version_id=str(previous_id) if previous_id else None,
            previous_version=previous_number,
            published_at=published_at,
            published_by=actor,
            audit_id=audit_id,
        )

    # =====================================================================
    # Diff
    # =====================================================================

    async def diff(self, agent_slug: str, from_version: int, to_version: int) -> VersionDiff:
        """What changed between two versions, shaped as a review.

        Which steps arrived, which left, which moved, and for the ones that
        stayed, only the fields that differ. A whole-object dump of two 40-step
        versions is technically a diff and practically unreadable.
        """

        before = await self.get_version(agent_slug, from_version)
        after = await self.get_version(agent_slug, to_version)

        defaults: list[FieldChange] = []
        for field, old_value in before.defaults.model_dump().items():
            new_value = getattr(after.defaults, field)
            if old_value != new_value:
                defaults.append(FieldChange(field=field, before=old_value, after=new_value))

        before_steps = {step.step_id: step for step in before.steps}
        after_steps = {step.step_id: step for step in after.steps}

        added = [s for s in after_steps if s not in before_steps]
        removed = [s for s in before_steps if s not in after_steps]

        changed: list[StepChange] = []
        moved: list[StepChange] = []
        prompt_bodies = await self._prompt_bodies(
            [step.prompt_version_id for step in (*before.steps, *after.steps)]
        )

        for step_id in after_steps:
            if step_id not in before_steps:
                continue
            old_step, new_step = before_steps[step_id], after_steps[step_id]
            fields = self._step_field_changes(old_step, new_step)
            change = StepChange(
                step_id=step_id,
                position_before=old_step.position,
                position_after=new_step.position,
                fields=fields,
            )
            if old_step.prompt_version_id != new_step.prompt_version_id:
                diff_text, truncated = self._prompt_diff(
                    prompt_bodies.get(old_step.prompt_version_id or "", ""),
                    prompt_bodies.get(new_step.prompt_version_id or "", ""),
                    step_id,
                )
                change.prompt_diff = diff_text
                change.prompt_diff_truncated = truncated

            if fields or change.prompt_diff:
                changed.append(change)
            elif old_step.position != new_step.position:
                moved.append(change)

        return VersionDiff(
            agent_slug=agent_slug,
            from_version=from_version,
            to_version=to_version,
            defaults=defaults,
            steps_added=added,
            steps_removed=removed,
            steps_changed=changed,
            steps_moved=moved,
        )

    def _step_field_changes(
        self, old_step: VersionStepRead, new_step: VersionStepRead
    ) -> list[FieldChange]:
        """Compared field by field, with `position` excluded on purpose.

        A step that only moved is reported as moved, not as changed -- a
        reorder and an edit need different attention from a reviewer, and
        lumping them together means every reorder looks like forty edits.
        """

        changes: list[FieldChange] = []
        old_dump = old_step.model_dump(mode="json")
        new_dump = new_step.model_dump(mode="json")
        for field in old_dump:
            if field in {"position", "prompt_content_hash"}:
                continue
            if old_dump[field] != new_dump.get(field):
                changes.append(
                    FieldChange(field=field, before=old_dump[field], after=new_dump.get(field))
                )
        return changes

    async def _prompt_bodies(self, prompt_version_ids: list[str | None]) -> dict[str, str]:
        wanted = [pid for pid in prompt_version_ids if pid]
        if not wanted:
            return {}
        rows = await self._db.fetch(
            "select id, content from prompt_versions where id = any($1::uuid[])", wanted
        )
        return {str(row["id"]): row["content"] for row in rows}

    def _prompt_diff(self, before: str, after: str, step_id: str) -> tuple[str, bool]:
        lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"{step_id} (before)",
                tofile=f"{step_id} (after)",
                n=2,
            )
        )
        text = "".join(lines)
        if len(text) > MAX_PROMPT_DIFF_CHARS:
            return text[:MAX_PROMPT_DIFF_CHARS], True
        return text, False

    # =====================================================================
    # Shared helpers
    # =====================================================================

    async def _require_agent(self, agent_slug: str) -> asyncpg.Record:
        row = await self._db.fetchrow(
            "select slug, agent_class_code, published_version_id from agents where slug = $1",
            agent_slug,
        )
        if row is None:
            raise ResourceNotFoundError("agent", agent_slug)
        return row

    async def _require_version(self, agent_slug: str, version: int) -> asyncpg.Record:
        row = await self._db.fetchrow(
            """
            select v.*, s.lifecycle
              from agent_versions v
              join agent_version_state s on s.id = v.id
             where v.agent_slug = $1 and v.version = $2
            """,
            agent_slug,
            version,
        )
        if row is None:
            await self._require_agent(agent_slug)  # 404 on the agent first, if that is why
            raise ResourceNotFoundError(f"version {version} of agent", agent_slug)
        return row

    @staticmethod
    def _require_draft(row: asyncpg.Record) -> None:
        if row["status"] != "draft":
            raise InvalidStateError(
                f"version {row['version']} of '{row['agent_slug']}' is frozen. A frozen "
                "version never changes -- clone it into a new draft "
                "(POST .../versions with from_version) and edit that."
            )

    @staticmethod
    async def _version_number_of(
        connection: asyncpg.Connection, version_id: Any
    ) -> int | None:
        if version_id is None:
            return None
        result = await connection.fetchval(
            "select version from agent_versions where id = $1", version_id
        )
        return int(result) if result is not None else None

    @staticmethod
    async def _audit(
        connection: asyncpg.Connection,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        agent_slug: str | None = None,
        client_slug: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> int:
        """One append-only row. In the same transaction as what it records.

        Outside the transaction it would be a log of things that were attempted;
        inside it, it is a log of things that happened.
        """

        return int(
            await connection.fetchval(
                """
                insert into audit_log (
                    actor, action, entity_type, entity_id, agent_slug, client_slug,
                    before, after, note
                ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                returning id
                """,
                actor,
                action,
                entity_type,
                entity_id,
                agent_slug,
                client_slug,
                # Passed as dicts, NOT json.dumps'd: the pool installs a jsonb
                # codec whose encoder is json.dumps, so pre-serialising here
                # stored a JSON *string containing JSON* -- and every reader got
                # a str back where it expected an object. Caught by the first
                # test that read an audit row rather than just counting them.
                before,
                after,
                note,
            )
        )
