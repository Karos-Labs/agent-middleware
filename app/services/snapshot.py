"""Resolving a published agent version into a frozen ExecutionSnapshot.

S6 / SCRUM-219 — the only step that genuinely changes the architecture.

Before: the message names an agent and the engine assembles the rest as it
goes, reading prompts out of one of its three PromptStores and the definition
out of Firestore. A prompt edited between dispatch and execution changes a run
nobody was told about; a queued backlog is a set of runs whose configuration is
still moving; and cost attribution prices a model against a hard-coded table
that falls back to Sonnet on a miss.

After: the run carries everything. Postgres leaves the run path entirely.

## What "frozen" has to mean to be worth anything

Three properties, and each one is a test:

1. **Resolved once.** Editing the prompt after dispatch does not change the
   run. That is the whole point.
2. **Re-used on resume.** A `batch_review` gate answered three days later
   executes against the snapshot it started with -- the longest-lived run in
   the system is the one most likely to have its configuration edited
   mid-flight. Resolving again on resume is a bug, not an optimisation.
3. **Complete.** If the engine still has to look one thing up, the guarantee is
   gone for that thing. So the prompt BODY travels, the model is resolved to
   `providerModelName` + region, and the price travels with it.

## Size, and why both transports exist from day one

Measured on a representative snapshot: the largest real agent (reddit-agent, 24
stages) is 0.68MB base64 on the wire, and the ticket's 40 x 20KB case is
1.12MB, against Pub/Sub's 10MB. Comfortable at ten times the largest agent that
exists.

So inline is what will actually be used -- which is exactly why `snapshotUri`
is implemented now rather than when it is needed. A consumer that handles only
inline passes every test and works for months, and then the first agent to
outgrow the ceiling needs producer and consumer changed together, at the moment
somebody is trying to ship an agent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from app.api.schemas.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    ExecutionSnapshot,
    SnapshotBounds,
    SnapshotDefaults,
    SnapshotModel,
    SnapshotPricing,
    SnapshotPrompt,
    SnapshotReference,
    SnapshotSelfCritique,
    SnapshotStep,
    SnapshotTool,
)
from app.core.exceptions import IncompleteAgentConfigurationError, ResourceNotFoundError
from app.db.postgres import ConfigDatabase

logger = logging.getLogger(__name__)

#: Above this many bytes of base64, the snapshot travels by URI instead.
#:
#: Half of Pub/Sub's 10MB ceiling, deliberately: a snapshot that grows between
#: the size check and the publish cannot cross the real limit. Base64 is the
#: unit because that is what the message actually carries -- a flat 4/3
#: inflation the ticket's arithmetic omits.
INLINE_LIMIT_BYTES = 5_000_000

#: 4/3, plus padding. Applied to the JSON length rather than encoding twice.
BASE64_RATIO = 4 / 3

#: The engine's two spellings for one set of step kinds, reconciled. Anything
#: unrecognised becomes "code": a step the consumer cannot classify should be
#: run as a deterministic one rather than handed to a model.
_STEP_KIND: dict[str, Literal["ai", "code", "gate"]] = {
    "ai": "ai",
    "agent": "ai",
    "code": "code",
    "gate": "gate",
}


def wire_size(snapshot: ExecutionSnapshot) -> int:
    """What this snapshot will occupy in the message, base64 included."""

    return int(snapshot.byte_size() * BASE64_RATIO) + 4


class SnapshotResolver:
    """Turns a published-or-pinned agent version into a frozen snapshot."""

    def __init__(self, database: ConfigDatabase) -> None:
        self._db = database

    async def resolve(
        self, agent_slug: str, client_slug: str, *, stage_models: dict[str, str] | None = None
    ) -> ExecutionSnapshot:
        """The version this client would run, frozen.

        ``stage_models`` is applied HERE and then frozen (C6 §9.3). The message
        already carries a per-stage model override, and if the snapshot also
        resolved each step's model the engine would need a merge rule for two
        fields deciding the same thing. It should not have one: the control
        plane applies the override, then freezes, and the snapshot is the
        single answer to "what model did this step use".
        """

        async with self._db.connection() as connection:
            agent = await connection.fetchrow(
                """
                select a.slug, a.agent_class_code, a.capabilities,
                       a.published_version_id, c.pinned_version_id, c.enabled
                  from agents a
                  left join client_agent_config c
                         on c.agent_slug = a.slug and c.client_slug = $2
                 where a.slug = $1
                """,
                agent_slug,
                client_slug,
            )
            if agent is None:
                raise ResourceNotFoundError("agent", agent_slug)

            if agent["pinned_version_id"] is not None and agent["enabled"]:
                version_id, resolved_from = agent["pinned_version_id"], "client_pinned"
            elif agent["published_version_id"] is not None:
                version_id, resolved_from = agent["published_version_id"], "published"
            else:
                # Deliberately not a fall back to a draft: a draft is editable,
                # and the whole contract is that a running configuration is not.
                raise IncompleteAgentConfigurationError(
                    f"agent '{agent_slug}' has no published version, so there is nothing "
                    "to run. Publish one (POST /config/agents/{slug}/versions/{v}/publish); "
                    "a draft is editable and cannot be dispatched."
                )

            version = await connection.fetchrow(
                "select id, version, status, default_model_id, default_provider_policy, "
                "dedupe_against_history, agent_step_timeout_ms "
                "from agent_versions where id = $1",
                version_id,
            )
            if version is None:  # pragma: no cover - the FK makes this unreachable
                raise ResourceNotFoundError("agent version", str(version_id))
            if version["status"] != "frozen":
                raise IncompleteAgentConfigurationError(
                    f"agent '{agent_slug}' points at a draft version, which the database "
                    "is supposed to refuse; treat this as data corruption rather than a "
                    "configuration problem"
                )

            steps = await self._resolve_steps(
                connection, version_id, version["default_model_id"], stage_models or {}
            )

        return ExecutionSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=str(uuid.uuid4()),
            agent_slug=agent_slug,
            agent_version_id=str(version["id"]),
            agent_version=version["version"],
            resolved_at=datetime.now(UTC),
            resolved_from=resolved_from,  # type: ignore[arg-type]
            client_slug=client_slug,
            agent_class=agent["agent_class_code"],
            capabilities=list(agent["capabilities"] or []),
            defaults=SnapshotDefaults(
                model_id=version["default_model_id"],
                provider_policy=version["default_provider_policy"],
                agent_step_timeout_ms=version["agent_step_timeout_ms"],
                dedupe_against_history=version["dedupe_against_history"],
            ),
            steps=steps,
        )

    async def _resolve_steps(
        self,
        connection: asyncpg.Connection,
        version_id: Any,
        default_model_id: str | None,
        stage_models: dict[str, str],
    ) -> list[SnapshotStep]:
        """Every step, with every reference followed.

        One query per concern rather than one per step: a 40-step version would
        otherwise be 120 round trips to build one message, and the resolver
        sits on the dispatch path.
        """

        rows = await connection.fetch(
            """
            select s.id, s.step_id, s.position, s.kind_code, s.description,
                   s.model_id, s.provider_policy, s.fallback_model_id,
                   s.output_schema, s.max_steps, s.max_tokens, s.max_malformed_turns,
                   s.self_critique_gate_tool, s.self_critique_max_revisions,
                   s.self_critique_args, s.language, s.code, s.code_timeout_ms,
                   s.is_gate, s.gate_kind, s.skill_ref, s.config,
                   pv.id as prompt_version_id, pv.version as prompt_version,
                   pv.content, pv.content_hash, p.prompt_key
              from agent_version_steps s
              left join prompt_versions pv on pv.id = s.prompt_version_id
              left join prompts p on p.id = pv.prompt_id
             where s.version_id = $1
             order by s.position
            """,
            version_id,
        )
        if not rows:
            raise IncompleteAgentConfigurationError(
                "the published version has no steps, so a run on it would do nothing"
            )

        tool_rows = await connection.fetch(
            """
            select tc.step_row_id, tc.tool_code, tc.config, t.version
              from tool_config tc
              join tools t on t.code = tc.tool_code
              join agent_version_steps s on s.id = tc.step_row_id
             where tc.scope = 'step' and s.version_id = $1
             order by tc.tool_code
            """,
            version_id,
        )
        tools_by_step: dict[Any, list[SnapshotTool]] = {}
        for tool in tool_rows:
            tools_by_step.setdefault(tool["step_row_id"], []).append(
                SnapshotTool(
                    name=tool["tool_code"],
                    # Travels into every telemetry record (RFC-01 §9.1 rule 5),
                    # so the version that was granted is the version recorded.
                    version=tool["version"],
                    config=tool["config"] or {},
                )
            )

        wanted_models = {
            model_id
            for row in rows
            for model_id in (
                stage_models.get(row["step_id"]) or row["model_id"] or default_model_id,
                row["fallback_model_id"],
            )
            if model_id
        }
        models = await self._load_models(connection, wanted_models)

        steps: list[SnapshotStep] = []
        for row in rows:
            # `engine_stages.json` calls a model step "agent"; the definition
            # schema calls it "ai" and defaults a stage with no kind to it. The
            # snapshot uses the definition's word, because that is what the
            # consumer parses -- and naming a concept differently on each side
            # of a wire is how a feature ships looking complete and does
            # nothing (the per-stage model picker did exactly that).
            kind = _STEP_KIND.get(str(row["kind_code"]), "code")

            prompt = None
            if row["prompt_version_id"] is not None:
                prompt = SnapshotPrompt(
                    prompt_key=row["prompt_key"],
                    prompt_version_id=str(row["prompt_version_id"]),
                    version=row["prompt_version"],
                    content_hash=row["content_hash"],
                    content=row["content"],
                )

            model = None
            chosen = stage_models.get(row["step_id"]) or row["model_id"] or default_model_id
            if chosen:
                model = self._model_for(
                    models,
                    chosen,
                    row["provider_policy"] or "pinned",
                    row["fallback_model_id"],
                )

            steps.append(
                SnapshotStep(
                    step_id=row["step_id"],
                    position=row["position"],
                    kind=kind,
                    description=row["description"] or "",
                    prompt=prompt,
                    model=model,
                    allowed_tools=tools_by_step.get(row["id"], []),
                    output_schema=row["output_schema"],
                    bounds=SnapshotBounds(
                        max_steps=row["max_steps"],
                        max_tokens=row["max_tokens"],
                        max_malformed_turns=row["max_malformed_turns"],
                    ),
                    self_critique=(
                        SnapshotSelfCritique(
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
                    config=row["config"] or {},
                )
            )
        return steps

    async def _load_models(
        self, connection: asyncpg.Connection, model_ids: set[str]
    ) -> dict[str, asyncpg.Record]:
        if not model_ids:
            return {}
        rows = await connection.fetch(
            "select model_id, provider_model_name, vendor, route, region, "
            "input_per_1m, output_per_1m, cached_input_per_1m "
            "from models where model_id = any($1::text[])",
            sorted(model_ids),
        )
        found = {row["model_id"]: row for row in rows}
        missing = model_ids - set(found)
        if missing:
            # Publish validates this, so reaching it means the catalog changed
            # after the version was frozen. Refusing here is the right trade:
            # the alternative is a run whose cost cannot be computed.
            raise IncompleteAgentConfigurationError(
                "the frozen version names model(s) that are no longer in the catalog: "
                f"{', '.join(sorted(missing))}. The version was valid when it was "
                "published, so this is a catalog change rather than a bad version."
            )
        return found

    @staticmethod
    def _model_for(
        models: dict[str, asyncpg.Record],
        model_id: str,
        provider_policy: str,
        fallback_model_id: str | None,
    ) -> SnapshotModel:
        row = models[model_id]
        return SnapshotModel(
            model_id=row["model_id"],
            provider_model_name=row["provider_model_name"],
            vendor=row["vendor"],
            route=row["route"],
            region=row["region"],
            provider_policy=provider_policy,
            # Enforced rather than trusted: the engine rejects a fallback
            # declared alongside `pinned`, so emitting one here would produce a
            # snapshot it refuses.
            fallback_model=None if provider_policy == "pinned" else fallback_model_id,
            pricing=SnapshotPricing(
                input_per_1m=float(row["input_per_1m"]),
                output_per_1m=float(row["output_per_1m"]),
                cached_input_per_1m=(
                    float(row["cached_input_per_1m"])
                    if row["cached_input_per_1m"] is not None
                    else None
                ),
            ),
        )


class SnapshotTransport:
    """Decides whether a snapshot travels inline or by URI, and offloads it.

    Both paths exist from the first release that has either. Inline is what
    will be used; the URI path is here so the day it is needed is a
    configuration change and not a coordinated deploy of two repositories.
    """

    def __init__(self, workspace: Any | None, limit: int = INLINE_LIMIT_BYTES) -> None:
        self._workspace = workspace
        self._limit = limit

    def fits_inline(self, snapshot: ExecutionSnapshot) -> bool:
        return wire_size(snapshot) <= self._limit

    async def offload(self, snapshot: ExecutionSnapshot) -> SnapshotReference:
        """Write the snapshot to the workspace bucket and describe it.

        Gzipped, unlike the inline form: nobody reads a GCS object by eye out
        of a dead-letter queue, so the argument against compressing the message
        does not apply to the object.
        """

        if self._workspace is None:
            raise IncompleteAgentConfigurationError(
                f"this snapshot is {wire_size(snapshot):,} bytes on the wire, over the "
                f"{self._limit:,} inline limit, and no artifacts bucket is configured to "
                "offload it to (GCS_ARTIFACTS_BUCKET is unset)"
            )

        body = snapshot.model_dump_json(by_alias=True).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        path = f"snapshots/{snapshot.snapshot_id}.json.gz"
        self._workspace.write_bytes(path, gzip.compress(body, 6))

        logger.info(
            "snapshot %s offloaded to %s (%d bytes, %d gzipped)",
            snapshot.snapshot_id,
            path,
            len(body),
            len(gzip.compress(body, 6)),
        )
        return SnapshotReference(
            snapshot_id=snapshot.snapshot_id,
            uri=self._workspace.uri_for(path),
            sha256=digest,
            byte_size=len(body),
        )


def snapshot_message_fields(
    snapshot: ExecutionSnapshot, reference: SnapshotReference | None
) -> dict[str, Any]:
    """The keys this snapshot adds to the queue message.

    Exactly one of `snapshot` / `snapshotUri`, never both -- a consumer handed
    both has to choose, and whichever it picks is somebody's surprise.
    """

    if reference is not None:
        return {
            "snapshotUri": reference.uri,
            "snapshotSha256": reference.sha256,
            "snapshotId": reference.snapshot_id,
        }
    return {
        "snapshot": json.loads(snapshot.model_dump_json(by_alias=True)),
        "snapshotId": snapshot.snapshot_id,
    }
