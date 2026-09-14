"""Import the portal's two scheduling collections into ``config.schedules`` (S10).

One-way, idempotent, reversible -- S5's shape. ``source_system`` /
``source_id`` carry where each row came from, so a second run writes nothing
for what is already here, and nothing here writes back to Firestore.

## What each collection says about money, and what this refuses to guess

``scheduledRuns`` (drained by ``/api/scheduler``) bills nobody: the route passes
``charge: null`` for every fire. So a row from it is imported with
``bill_client_credits = false`` and ``billing_intent_source = 'inferred_at_import'``.
That is not a guess, it is a description of what the system does today, and
the intent source makes every such row findable for the day someone decides
whether these should start billing.

``plannedScheduledRuns`` (drained by ``/api/run-scheduled``) carries
``billClientCredits`` when a human stated it. A row that has it is imported as
``explicit``. A row that does NOT have it is REFUSED, with the reason. The
portal falls back to an actor test on those rows, and reading that fallback
as ``false`` would silently stop charging a fleet of live schedules -- the
portal's own comment on the field says exactly this. Refusing is the S5
answer to "the source does not say": list it, and let a person say.

Two more refusals, for the same reason:

* ``cadence: "once"`` -- a planned single run is a queued job, not a
  schedule, and the schema's cadence vocabulary is daily/weekly/monthly. The
  row is listed; where it belongs is a product decision.
* No ``timeZone`` -- the portal falls back to the runtime's local zone, which
  means the row fires at a different wall clock depending on where the
  container ran. ``--assume-time-zone`` imports these with a stated zone;
  without it they are refused, because a schedule's zone is its intent and
  this script does not have the client's.

## Identity mapping

``clientId`` is a Firestore document id; ``schedules.client_slug`` is the
workspace slug. The bridge is ``clients/{id}.agentsRepoSlug``, the same read
``ClientContextProjector`` makes. ``agentId`` / ``customAgentId`` name a
``customAgents`` document; S5 imported those into ``config.agents`` with
``source_id`` set, so the slug is a lookup. Either lookup failing refuses the
row rather than inventing a slug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.db.firestore import FirestoreDB, snapshot_to_dict
from app.db.postgres import ConfigDatabase
from app.services.schedules import next_fire_at

logger = logging.getLogger(__name__)

CLIENTS = "clients"
SCHEDULED_RUNS = "scheduledRuns"
PLANNED_SCHEDULED_RUNS = "plannedScheduledRuns"
SOURCE_SYSTEMS = (PLANNED_SCHEDULED_RUNS, SCHEDULED_RUNS)


@dataclass
class ScheduleOutcome:
    source_system: str
    source_id: str
    result: str  # imported | already_present | refused
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScheduleImportReport:
    outcomes: list[ScheduleOutcome] = field(default_factory=list)

    def add(self, outcome: ScheduleOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def refused(self) -> list[ScheduleOutcome]:
        return [o for o in self.outcomes if o.result == "refused"]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.result] = counts.get(outcome.result, 0) + 1
        return counts


class ScheduleImporter:
    def __init__(
        self,
        firestore: FirestoreDB,
        config_db: ConfigDatabase,
        *,
        assume_time_zone: str | None = None,
    ) -> None:
        self._firestore = firestore
        self._db = config_db
        self._assume_time_zone = assume_time_zone
        self._slug_cache: dict[str, str | None] = {}

    async def run(self, *, actor: str, dry_run: bool = False) -> ScheduleImportReport:
        report = ScheduleImportReport()
        for system in SOURCE_SYSTEMS:
            async for snapshot in self._firestore.collection(system).stream():
                document = snapshot_to_dict(snapshot)
                source_id = str(document["id"])
                try:
                    outcome = await self._import_one(
                        system, source_id, document, actor=actor, dry_run=dry_run
                    )
                except Exception as exc:  # noqa: BLE001 - one bad document must not stop the rest
                    outcome = ScheduleOutcome(
                        system, source_id, "refused", [f"{type(exc).__name__}: {exc}"]
                    )
                    logger.warning("%s/%s refused: %s", system, source_id, exc)
                report.add(outcome)
        return report

    async def _import_one(
        self,
        system: str,
        source_id: str,
        document: dict[str, Any],
        *,
        actor: str,
        dry_run: bool,
    ) -> ScheduleOutcome:
        present = await self._db.fetchval(
            "select 1 from schedules where source_system = $1 and source_id = $2",
            system,
            source_id,
        )
        if present:
            return ScheduleOutcome(system, source_id, "already_present")

        row = await self._translate(system, source_id, document)
        if isinstance(row, list):
            return ScheduleOutcome(system, source_id, "refused", row)

        if dry_run:
            return ScheduleOutcome(system, source_id, "imported")

        await self._db.execute(
            """
            insert into schedules (
                client_slug, agent_slug, label, prompt, cadence, hour, minute,
                weekdays, day_of_month, time_zone, outputs_per_run,
                bill_client_credits, billing_intent_source, status,
                next_run_at, last_run_at, last_job_id, last_error, last_error_at,
                source_system, source_id, imported_at, created_by
            ) values (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20, $21, now(), $22
            )
            """,
            row["client_slug"], row["agent_slug"], row["label"], row["prompt"],
            row["cadence"], row["hour"], row["minute"], row["weekdays"], row["day_of_month"],
            row["time_zone"], row["outputs_per_run"],
            row["bill_client_credits"], row["billing_intent_source"], row["status"],
            row["next_run_at"], row["last_run_at"], row["last_job_id"],
            row["last_error"], row["last_error_at"],
            system, source_id, actor,
        )
        return ScheduleOutcome(system, source_id, "imported")

    # --- Translation ------------------------------------------------------------

    async def _translate(
        self, system: str, source_id: str, doc: dict[str, Any]
    ) -> dict[str, Any] | list[str]:
        """A ``schedules`` row, or the list of reasons this document is refused."""

        problems: list[str] = []

        client_slug = await self._client_slug(str(doc.get("clientId") or ""))
        if client_slug is None:
            problems.append(
                f"clientId {doc.get('clientId')!r} has no clients document with an agentsRepoSlug"
            )

        agent_ref = (
            doc.get("customAgentId") if system == PLANNED_SCHEDULED_RUNS else doc.get("agentId")
        )
        agent_slug = await self._agent_slug(str(agent_ref or ""))
        if agent_slug is None:
            problems.append(
                f"agent {agent_ref!r} has not been imported into config.agents (S5 source_id)"
            )

        if system == SCHEDULED_RUNS:
            shape = self._shape_scheduled_run(doc, problems)
        else:
            shape = self._shape_planned_run(doc, problems)

        if problems:
            return problems
        assert client_slug is not None and agent_slug is not None

        cursor = doc.get("nextRunAt")
        now = datetime.now(UTC)
        if isinstance(cursor, int | float):
            next_run_at = datetime.fromtimestamp(cursor / 1000, tz=UTC)
        elif isinstance(cursor, datetime):
            next_run_at = cursor
        else:
            next_run_at = next_fire_at(
                cadence=shape["cadence"], hour=shape["hour"], minute=shape["minute"],
                time_zone=shape["time_zone"], after=now,
                weekdays=shape["weekdays"], day_of_month=shape["day_of_month"],
            )

        return {
            "client_slug": client_slug,
            "agent_slug": agent_slug,
            "label": str(doc.get("label") or doc.get("agentName") or agent_slug)[:255],
            "prompt": str(doc.get("prompt") or ""),
            **shape,
            "next_run_at": next_run_at,
            "last_run_at": _millis(doc.get("lastRunAt")),
            "last_job_id": doc.get("lastJobId") or None,
            "last_error": (doc.get("lastError") or None),
            "last_error_at": _millis(doc.get("lastErrorAt")),
        }

    def _shape_scheduled_run(self, doc: dict[str, Any], problems: list[str]) -> dict[str, Any]:
        """``scheduledRuns``: a weekly day set + wall clock in a zone; never billed."""

        cadence = doc.get("cadence") or {}
        days = cadence.get("daysOfWeek") if isinstance(cadence, dict) else None
        if not isinstance(days, list) or not days:
            problems.append("cadence.daysOfWeek is missing or empty")
            days = []
        stated_zone = cadence.get("timezone") if isinstance(cadence, dict) else None
        time_zone = stated_zone or self._assume_time_zone
        if not time_zone:
            problems.append("cadence.timezone is absent and no --assume-time-zone was given")
        return {
            "cadence": "weekly",
            "hour": int(cadence.get("hour", 0)) if isinstance(cadence, dict) else 0,
            "minute": int(cadence.get("minute", 0)) if isinstance(cadence, dict) else 0,
            "weekdays": sorted({int(d) for d in days}) or None,
            "day_of_month": None,
            "time_zone": time_zone or "",
            "outputs_per_run": 1,
            # What /api/scheduler does today, described rather than guessed.
            "bill_client_credits": False,
            "billing_intent_source": "inferred_at_import",
            "status": "active" if doc.get("enabled", True) else "paused",
        }

    def _shape_planned_run(self, doc: dict[str, Any], problems: list[str]) -> dict[str, Any]:
        """``plannedScheduledRuns``: daily/weekly/monthly with a stated money switch."""

        cadence = doc.get("cadence")
        if cadence == "once":
            problems.append(
                "cadence 'once' is a queued single run, not a schedule; decide where it belongs"
            )
        elif cadence not in ("daily", "weekly", "monthly"):
            problems.append(f"cadence {cadence!r} is not daily/weekly/monthly")

        bill = doc.get("billClientCredits")
        if not isinstance(bill, bool):
            problems.append(
                "billClientCredits is absent; the portal falls back to an actor test here, "
                "and this import will not read that fallback as a decision. State it first."
            )

        time_zone = doc.get("timeZone") or self._assume_time_zone
        if not time_zone:
            problems.append("timeZone is absent and no --assume-time-zone was given")

        weekdays: list[int] | None = None
        if cadence == "weekly":
            single = [doc["weekday"]] if doc.get("weekday") is not None else []
            raw = doc.get("weekdays") or single
            weekdays = sorted({int(d) for d in raw}) or None
            if weekdays is None:
                problems.append("weekly cadence with neither weekdays nor weekday")
        day_of_month = (
            int(doc["dayOfMonth"]) if cadence == "monthly" and doc.get("dayOfMonth") else None
        )
        if cadence == "monthly" and day_of_month is None:
            problems.append("monthly cadence without dayOfMonth")

        stated = doc.get("status")
        status = stated if stated in ("active", "paused", "completed") else "active"
        return {
            "cadence": cadence if cadence in ("daily", "weekly", "monthly") else "daily",
            "hour": int(doc.get("hour", 0)),
            "minute": int(doc.get("minute", 0)),
            "weekdays": weekdays,
            "day_of_month": day_of_month,
            "time_zone": time_zone or "",
            "outputs_per_run": max(1, int(doc.get("outputsPerRun") or 1)),
            "bill_client_credits": bool(bill),
            "billing_intent_source": "explicit",
            "status": status,
        }

    # --- Lookups ------------------------------------------------------------------

    async def _client_slug(self, client_id: str) -> str | None:
        if not client_id:
            return None
        if client_id not in self._slug_cache:
            snapshot = await self._firestore.document(CLIENTS, client_id).get()
            slug = (snapshot.to_dict() or {}).get("agentsRepoSlug") if snapshot.exists else None
            self._slug_cache[client_id] = str(slug) if slug else None
        return self._slug_cache[client_id]

    async def _agent_slug(self, source_id: str) -> str | None:
        if not source_id:
            return None
        return await self._db.fetchval(
            "select slug from agents where source_id = $1 order by imported_at desc limit 1",
            source_id,
        )


def _millis(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, datetime):
        return value
    return None
