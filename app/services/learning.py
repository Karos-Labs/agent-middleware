"""The learning loop's two directions (C7 / SCRUM-461, 462, 463).

``docs/contracts/C7-run-context.md``. Before a dispatch the middleware
PROJECTS what the platform has learned about a client into the engine's
workspace -- the C7 §2 JSON files under ``clients/<slug>/context/learning/``,
plus N4's derived ``sequence`` -- and after the run it COLLECTS the record it
wrote back
(``state/runs/<runId>.json``, ``state/<platform>/platform-state.json``) into
the Postgres tables of migration 0007, then projects again so the next run
sees it.

Both directions are best-effort from the dispatcher's and the portal's point
of view. A projection that fails must not fail the dispatch (C7 §4.1: a run
with nothing projected behaves exactly as before), and a collect that finds
no record is an ordinary answer -- the run may have been held, or be an
agent that predates the contract -- not an error.

The engine never reads Postgres. It reads these files, and only these files.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import ResourceNotFoundError
from app.db.workspace import WorkspaceStore
from app.services.learning_store import (
    FEEDBACK_ACTIONS,
    PLATFORMS,
    STATUS_FOR_ACTION,
    LearningStore,
    platform_for_product,
)
from app.services.runs import RunService
from app.services.sequencing import plan as plan_sequence

logger = logging.getLogger(__name__)

#: C7 §2, in the order the engine lists them in `readiness`, plus N4's
#: ``sequence`` at the end.
#:
#: ``sequence`` is DERIVED, not stored: it is the strategy map and the subject
#: window read through the rules in ``app.services.sequencing``. It is written
#: as a file anyway, for the same reason the others are -- the engine reads
#: files and never Postgres -- and it is written last so a reader who has the
#: map and the window can check the plan against them.
PLATFORM_KINDS: tuple[str, ...] = (
    "platform-state",
    "subject-window",
    "feedback",
    "what-works",
    "strategy-map",
    "craft",
    "sequence",
)

#: How many slots a projected plan covers. Six is one turn of the default mix
#: (D32: three attention, two expertise, one decide), so a reader can see the
#: whole shape of the mix in one file without the plan going so far ahead that
#: the first review cycle invalidates the tail.
SEQUENCE_SLOTS = 6
PREFERENCES_KIND = "preferences"


def learning_path(slug: str, platform: str | None, kind: str) -> str:
    """``clients/<slug>/context/learning/<platform>/<kind>.json`` (C7 §2)."""

    if platform is None:
        return f"clients/{slug}/context/learning/{kind}.json"
    return f"clients/{slug}/context/learning/{platform}/{kind}.json"


def run_record_path(slug: str, run_id: str) -> str:
    return f"clients/{slug}/state/runs/{run_id}.json"


def platform_state_path(slug: str, platform: str) -> str:
    return f"clients/{slug}/state/{platform}/platform-state.json"


def strategy_map_state_path(slug: str, platform: str) -> str:
    return f"clients/{slug}/state/{platform}/strategy-map.json"


def _serialise(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _content_hash(data: Any) -> str:
    """Of the serialised ``data`` alone, never of the envelope (C7 §2.0)."""

    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def build_envelope(
    kind: str,
    data: Any,
    *,
    platform: str | None,
    projected_by: str,
    projected_at: str,
    rows: int | None = None,
) -> dict[str, Any]:
    """The C7 §2.0 envelope."""

    source: dict[str, Any] = {
        "projectedAt": projected_at,
        "projectedBy": projected_by,
        "contentHash": _content_hash(data),
    }
    if rows is not None:
        source["rows"] = rows
    envelope: dict[str, Any] = {"kind": kind, "data": data, "source": source}
    if platform is not None:
        envelope["platform"] = platform
    return envelope


def envelope_is_current(existing: str | None, candidate: dict[str, Any]) -> bool:
    """Same ``contentHash`` → a no-op, so ``projectedAt`` is left alone.

    The same rule ``context_record_is_current`` applies to C1 documents, and
    for the same reason: the timestamp is what the readiness line measures
    freshness by, and rewriting an identical file on every dispatch would make
    every file look freshly changed.
    """

    if not existing:
        return False
    try:
        stored = json.loads(existing)
    except ValueError:
        return False
    if not isinstance(stored, dict):
        return False
    source = stored.get("source")
    if not isinstance(source, dict):
        return False
    return bool(source.get("contentHash")) and (
        source.get("contentHash") == candidate["source"]["contentHash"]
    )


def resolve_craft(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """C7 §2.7: the merged rule list with precedence resolved by the projector.

    Every active rule travels. ``overrides`` becomes the flat winner/loser
    list the engine renders; a pair whose loser is an L1 hard rule is dropped
    and logged, because a hard rule always wins and can never be a loser.
    A pair whose loser is not in the list is dropped silently -- it names a
    rule that was retired, and an override of nothing is nothing.
    """

    by_id = {r["id"]: r for r in rules}
    overrides: list[dict[str, str]] = []
    out: list[dict[str, Any]] = []
    for rule in rules:
        row = {
            k: v
            for k, v in rule.items()
            if k in ("id", "layer", "kind", "rule", "why", "metric", "sampleSize") and v is not None
        }
        out.append(row)
        for loser_id in rule.get("overrides") or []:
            loser = by_id.get(loser_id)
            if loser is None:
                continue
            if loser.get("kind") == "hard":
                logger.warning(
                    "craft rule %s claims to override hard rule %s; ignored (hard rules win)",
                    rule["id"],
                    loser_id,
                )
                continue
            overrides.append({"winner": rule["id"], "loser": loser_id})
    return {"rules": out, "overrides": overrides}


@dataclass
class FileOutcome:
    kind: str
    outcome: str  # created | updated | unchanged | skipped
    detail: str = ""
    rows: int | None = None


@dataclass
class LearningProjection:
    slug: str
    platform: str
    files: list[FileOutcome] = field(default_factory=list)

    @property
    def written(self) -> int:
        return sum(1 for f in self.files if f.outcome in ("created", "updated"))


@dataclass
class CollectResult:
    run_id: str
    collected: bool
    reason: str = ""
    client_slug: str | None = None
    platform: str | None = None
    subject_row_id: str | None = None
    record_changed: bool = False
    platform_state_collected: bool = False
    strategy_rows_collected: int = 0
    reprojected: int = 0


class LearningService:
    """Projects, collects and takes feedback. One instance per process."""

    def __init__(
        self,
        store: LearningStore,
        workspace: WorkspaceStore | None,
        runs: RunService,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._runs = runs

    @property
    def store(self) -> LearningStore:
        return self._store

    @property
    def can_project(self) -> bool:
        return self._workspace is not None

    # --- The view a run would read (served from Postgres, not GCS) ----------

    async def context(self, slug: str, platform: str) -> dict[str, Any]:
        """Every payload as the projector would write them, by kind.

        The portal's readiness page and a developer checking "what will the
        next run see" both want this without bucket access, so it is served
        from the tables the projection is built from.
        """

        settings = await self._store.settings(slug, platform)
        window = await self._store.subject_window(slug, platform, days=settings["antiRepeatDays"])
        feedback = await self._store.recent_feedback(slug, platform, limit=settings["feedbackRows"])
        craft = await self._store.craft_rules(slug, platform, sector=settings["sector"])
        strategy = await self._store.strategy_map(slug, platform)
        return {
            "platform": platform,
            "settings": settings,
            "platform-state": await self._store.platform_state(slug, platform),
            "subject-window": {"windowDays": settings["antiRepeatDays"], "rows": window},
            "feedback": {"rows": feedback},
            "what-works": None,  # absent until ingestion exists (02 §3.4)
            "strategy-map": strategy,
            "craft": resolve_craft(craft) if craft else None,
            "preferences": await self._store.preferences(slug),
            "sequence": self.sequence(strategy, window),
        }

    def sequence(
        self,
        strategy: dict[str, Any] | None,
        window: list[dict[str, Any]],
        *,
        slots: int = SEQUENCE_SLOTS,
    ) -> dict[str, Any] | None:
        """N4: which post goes in each of the next ``slots``, and why.

        Pure, and taking the map and the window it was already given rather than
        fetching them again: a plan built from a different read of the tables
        than the ``strategy-map`` and ``subject-window`` files beside it could
        disagree with them, and a reader would have no way to tell which was
        right.

        ``None`` when there is no map. A plan with no map behind it would be a
        list of empty slots, which reads as "we have nothing to say" rather than
        "nobody has built this client a map yet".
        """

        if not strategy or not strategy.get("rows"):
            return None
        plan = plan_sequence(
            slots=slots,
            map_rows=strategy["rows"],
            recent=window,
            default_mix=strategy.get("defaultMix"),
            # Requests, anchors and performance all have somewhere to come from
            # and no writer yet -- client requests are not a table, news anchors
            # are not ingested, and what-works is 02 §3.4. Passing nothing is
            # what says so; each one is a one-line change here when it lands.
        )
        return {"platform": strategy.get("platform"), **plan}

    # --- Projection (SCRUM-461, before dispatch) ----------------------------

    async def project(
        self, slug: str, platform: str, *, projected_by: str = "middleware-dispatch"
    ) -> LearningProjection:
        """Write the C7 §2 files for one client × platform. Never raises for data.

        Which files are written follows what each one means to the reader:

        * ``subject-window`` and ``feedback`` are windows, so an empty one IS
          the answer ("we looked; nothing in the window") and is written.
        * ``platform-state``, ``strategy-map``, ``craft`` and ``preferences``
          are stores, so with no rows there is nothing to say and the file is
          left absent -- the reader lists it under ``readiness.absent``.
        * ``sequence`` is derived from two of the above, so it is written when
          and only when there is a map to derive it from.
        * ``what-works`` is never written here: the ingestion that produces
          it does not exist yet (02 §3.4), and an empty file would claim it did.
        """

        result = LearningProjection(slug=slug, platform=platform)
        if self._workspace is None:
            result.files.append(
                FileOutcome("*", "skipped", "GCS_ARTIFACTS_BUCKET is not configured")
            )
            return result
        if platform not in PLATFORMS:
            result.files.append(FileOutcome("*", "skipped", f"{platform!r} is not a platform"))
            return result

        view = await self.context(slug, platform)
        projected_at = _now()

        def write(kind: str, data: Any, *, per_platform: bool, rows: int | None) -> None:
            envelope = build_envelope(
                kind,
                data,
                platform=platform if per_platform else None,
                projected_by=projected_by,
                projected_at=projected_at,
                rows=rows,
            )
            path = learning_path(slug, platform if per_platform else None, kind)
            assert self._workspace is not None
            existing = self._workspace.read_text(path)
            if envelope_is_current(existing, envelope):
                result.files.append(FileOutcome(kind, "unchanged", rows=rows))
                return
            self._workspace.write_text(path, _serialise(envelope))
            result.files.append(FileOutcome(kind, "updated" if existing else "created", rows=rows))

        window = view["subject-window"]
        write("subject-window", window, per_platform=True, rows=len(window["rows"]))
        feedback = view["feedback"]
        write("feedback", feedback, per_platform=True, rows=len(feedback["rows"]))

        state = view["platform-state"]
        if state:
            write("platform-state", state, per_platform=True, rows=None)
        else:
            result.files.append(FileOutcome("platform-state", "skipped", "no state collected yet"))

        strategy = view["strategy-map"]
        if strategy and strategy["rows"]:
            write("strategy-map", strategy, per_platform=True, rows=len(strategy["rows"]))
        else:
            result.files.append(FileOutcome("strategy-map", "skipped", "no map for this platform"))

        craft = view["craft"]
        if craft and craft["rules"]:
            write("craft", craft, per_platform=True, rows=len(craft["rules"]))
        else:
            result.files.append(FileOutcome("craft", "skipped", "no active craft rules"))

        result.files.append(FileOutcome("what-works", "skipped", "no ingestion yet (02 §3.4)"))

        # N4. Written only when it has a map to be built from -- see `sequence`.
        # `unfilled` rides along in the payload, so a run that finds a plan
        # shorter than its calendar can say the pool is empty rather than
        # quietly drafting whatever it likes for the slots past the end.
        sequence = view["sequence"]
        if sequence and sequence["slots"]:
            write("sequence", sequence, per_platform=True, rows=len(sequence["slots"]))
        else:
            result.files.append(
                FileOutcome("sequence", "skipped", "no strategy map to sequence from")
            )

        prefs = view["preferences"]
        if prefs and (
            prefs["neverTopics"]
            or prefs["standingInstructions"]
            or prefs["voiceNotes"]
            or prefs["likes"]
        ):
            write(PREFERENCES_KIND, prefs, per_platform=False, rows=None)
        else:
            result.files.append(FileOutcome(PREFERENCES_KIND, "skipped", "nothing set or derived"))

        logger.info(
            "Projected learning context for %s/%s: %d file(s) written",
            slug,
            platform,
            result.written,
        )
        return result

    async def project_for_dispatch(self, slug: str, product_id: str) -> LearningProjection | None:
        """The dispatcher's call: best-effort, and ``None`` for a non-platform agent."""

        platform = platform_for_product(product_id)
        if platform is None:
            return None
        try:
            return await self.project(slug, platform, projected_by="middleware-dispatch")
        except Exception:  # noqa: BLE001 -- C7 §4.1: projection never fails a dispatch
            logger.exception(
                "learning-context projection failed for %s/%s; dispatching without it",
                slug,
                platform,
            )
            return None

    # --- Collection (SCRUM-461, after the run) ------------------------------

    async def _resolve_run(self, run_id: str) -> tuple[dict[str, Any], str]:
        """Find the run and the id agent-engine wrote its state files under.

        TWO IDS NAME ONE RUN. This service mints a run id (a uuid, or one the
        portal supplied) and keys ``agent_runs`` on it. agent-engine derives
        its own from Pub/Sub's message id -- ``pubsub-<messageId>``, see its
        ``queue-consumer.ts`` -- and that is the id in every path it writes,
        ``state/runs/<runId>.json`` included.

        The portal only ever holds the engine's. It keys ``agentEngineRuns`` on
        it and drops the one dispatch returned, so reconcile can call collect
        with nothing else. Reading the record at ``state/runs/<our uuid>.json``
        therefore found nothing, every time, for every run -- a collector that
        answered "the run wrote no state file" about runs that had written one.

        Both spellings resolve here: an id of ours by direct lookup, an
        ``pubsub-`` id by its message id. The returned pair is (the run
        document, the id the engine used).
        """

        try:
            run = await self._runs.get(run_id)
        except ResourceNotFoundError:
            run = {}

        if run:
            message_id = run.get("pubsub_message_id")
            # A run of ours that was never published has no engine-side id and
            # no state file either; falling back to `run_id` keeps the "wrote
            # no state" answer below rather than raising here.
            published = isinstance(message_id, str) and bool(message_id)
            engine_run_id = f"pubsub-{message_id}" if published else run_id
            return run, engine_run_id

        if run_id.startswith("pubsub-"):
            found = await self._runs.find_by_pubsub_message_id(run_id[len("pubsub-") :])
            if found is not None:
                return found, run_id

        return {}, run_id

    async def collect(
        self, run_id: str, *, collected_by: str = "portal-reconcile"
    ) -> CollectResult:
        """Pull the run's state files into Postgres and project again.

        Idempotent on ``run_id``: the record is upserted by content hash, the
        subject row on its natural key, and the platform state whole. Calling
        this on every reconcile is the intended use.

        ``run_id`` may be either this service's run id or agent-engine's
        ``pubsub-<messageId>``; see ``_resolve_run``. What is stored, and what
        a later call is idempotent on, is always the engine's -- it is the one
        the state files, the deliverables and the portal all agree on.
        """

        if self._workspace is None:
            return CollectResult(run_id, False, "GCS_ARTIFACTS_BUCKET is not configured")

        run, engine_run_id = await self._resolve_run(run_id)
        slug = run.get("client_slug")

        record: dict[str, Any] | None = None
        if isinstance(slug, str):
            record = self._read_json(run_record_path(slug, engine_run_id))
        if record is None:
            return CollectResult(
                engine_run_id,
                False,
                (
                    f"run {run_id!r} is not registered here"
                    if not isinstance(slug, str)
                    else "the run wrote no state/runs/<runId>.json (held, failed, or an "
                    "agent that predates C7)"
                ),
                client_slug=slug if isinstance(slug, str) else None,
            )
        assert isinstance(slug, str)
        run_id = engine_run_id

        # The record names both (C7 §3.1); the run document names neither
        # directly (its agent is a Firestore id), so the record is the source.
        product_id = record.get("productId") if isinstance(record.get("productId"), str) else None
        platform = record.get("platform")
        if platform not in PLATFORMS:
            platform = platform_for_product(product_id)
        if platform is None:
            return CollectResult(run_id, False, "the record names no platform", client_slug=slug)

        result = CollectResult(run_id, True, client_slug=slug, platform=platform)
        deliverable = _obj(record.get("deliverable"))
        subject = _obj(record.get("subjectRow"))
        written_at = _parse_when(record.get("writtenAt"))

        async with self._store.db.transaction() as connection:
            result.record_changed = await self._store.upsert_run_record(
                connection,
                run_id=run_id,
                client_slug=slug,
                platform=platform,
                product_id=product_id,
                record=record,
                content_hash=_content_hash(record),
                collected_by=collected_by,
            )
            result.subject_row_id = await self._store.upsert_subject_row(
                connection,
                client_slug=slug,
                platform=platform,
                run_id=run_id,
                product_id=product_id,
                subject=subject,
                deliverable=deliverable,
                drafted_at=written_at,
            )
            strategy_row_id = subject.get("strategyRowId")
            if isinstance(strategy_row_id, str) and strategy_row_id:
                await self._store.mark_strategy_row_used(
                    connection,
                    client_slug=slug,
                    platform=platform,
                    row_id=strategy_row_id,
                    run_id=run_id,
                )

            state = self._read_json(platform_state_path(slug, platform))
            if state is not None:
                await self._store.upsert_platform_state(
                    connection, client_slug=slug, platform=platform, state=state, run_id=run_id
                )
                result.platform_state_collected = True

            await self._store.derive_preferences(connection, slug)

        # A strategy map a setup / first run built (SCRUM-464) travels the same
        # way. Its own transaction: the store's writer already is one.
        built = self._read_json(strategy_map_state_path(slug, platform))
        if built is not None and isinstance(built.get("rows"), list):
            source = built.get("source")
            audience = built.get("audience")
            mix = built.get("defaultMix")
            saved = await self._store.put_strategy_map(
                slug,
                platform,
                source=source if source in ("setup-run", "first-run", "manual") else "first-run",
                built_at=_parse_when(built.get("builtAt")),
                audience=audience if isinstance(audience, list) else [],
                default_mix=mix if isinstance(mix, dict) else None,
                rows=[r for r in built["rows"] if isinstance(r, dict)],
                replace=False,
            )
            result.strategy_rows_collected = len(saved["rows"])

        projection = await self.project(slug, platform, projected_by="middleware-collect")
        result.reprojected = projection.written
        logger.info(
            "Collected run %s for %s/%s: subject row %s, platform state %s",
            run_id,
            slug,
            platform,
            result.subject_row_id or "-",
            "yes" if result.platform_state_collected else "no",
        )
        return result

    # --- Feedback (SCRUM-463, from the portal) ------------------------------

    async def record_feedback(
        self,
        slug: str,
        *,
        platform: str,
        action: str,
        run_id: str | None,
        account: str | None,
        reason: str | None,
        original_text: str | None,
        final_text: str | None,
        actor: str | None,
        at: datetime | None,
        source: str = "portal",
        source_id: str | None = None,
        reproject: bool = True,
    ) -> dict[str, Any]:
        """Append to the log, move the subject row, re-derive, re-project.

        One transaction for the first three, so a review action can never
        exist without the subject row it moved (or vice versa).
        """

        if action not in FEEDBACK_ACTIONS:
            raise ValueError(f"unknown feedback action {action!r}")
        when = at or datetime.now(UTC)
        async with self._store.db.transaction() as connection:
            row = await self._store.append_feedback(
                connection,
                client_slug=slug,
                platform=platform,
                run_id=run_id,
                account=account,
                action=action,
                reason=reason,
                original_text=original_text,
                final_text=final_text,
                actor=actor,
                at=when,
                source=source,
                source_id=source_id,
            )
            moved = 0
            status = STATUS_FOR_ACTION.get(action)
            if run_id and status:
                moved = await self._store.set_subject_status(
                    connection,
                    client_slug=slug,
                    platform=platform,
                    run_id=run_id,
                    status=status,
                    at=when,
                )
            await self._store.derive_preferences(connection, slug)

        reprojected = 0
        if reproject and self._workspace is not None:
            try:
                projection = await self.project(slug, platform, projected_by="middleware-feedback")
                reprojected = projection.written
            except Exception:  # noqa: BLE001 -- the row is saved; the projection can be redone
                logger.exception("re-projection after feedback failed for %s/%s", slug, platform)

        return {
            "row": row,
            "duplicate": row is None,
            "subjectRowsMoved": moved,
            "reprojected": reprojected,
        }

    # --- Helpers ------------------------------------------------------------

    def _read_json(self, path: str) -> dict[str, Any] | None:
        assert self._workspace is not None
        text = self._workspace.read_text(path)
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            logger.warning("unreadable JSON at %s; treated as absent", path)
            return None
        return parsed if isinstance(parsed, dict) else None


__all__ = [
    "PLATFORM_KINDS",
    "SEQUENCE_SLOTS",
    "PREFERENCES_KIND",
    "CollectResult",
    "FileOutcome",
    "LearningProjection",
    "LearningService",
    "build_envelope",
    "envelope_is_current",
    "learning_path",
    "platform_state_path",
    "resolve_craft",
    "run_record_path",
    "strategy_map_state_path",
]
