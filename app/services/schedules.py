"""Schedules: the two scheduling systems, merged (S10 / SCRUM-223).

karosCMO has two. ``plannedScheduledRuns`` is drained by ``/api/run-scheduled``
and hands the submit core an explicit ``bill`` decision per row. ``scheduledRuns``
is drained by ``/api/scheduler`` and passes ``charge: null`` UNCONDITIONALLY --
every one of its fires is free to the client, absent from the credit ledger,
and real model spend that no invoice traces.

This module is the middleware half of the merge. What it settles:

* **The definition and the run state live in one Postgres row**
  (``config.schedules``, S2). ``next_run_at`` and ``fire_in_flight_since`` are
  run state, not configuration, but splitting them would mean the claim
  transaction touching two tables to advance one cursor.

* **A fire is claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``.** That is the
  actual reason to move scheduling here. Two ticks racing over the same due
  rows each get a disjoint set; a claim that advances the cursor and stamps
  ``fire_in_flight_since`` is one statement, not a compare-and-set that a
  concurrent reader can slip between.

* **There is no way to claim a fire without receiving a stated billing
  decision.** ``bill_client_credits`` is NOT NULL with no default, and every
  claimed row carries it. The executor -- today the portal's cron, tomorrow
  this service once billing moves -- gets ``bill: true`` or ``bill: false``
  and nothing else, which is exactly the field ``charge: null`` never asked.

What it does NOT settle, and why: the fire itself. Billing lives in the
portal's submit core and credit ledger, so submitting the job and charging for
it stays there until that moves. The cutover is therefore: the portal's two
crons call ``claim`` here instead of reading their two collections, and
``settle`` here instead of writing back. That is the coordination with Tomer
the ticket names; ``docs/S10-scheduler-cutover.md`` is the plan.

## Cadence math

``next_fire_at`` follows the portal's ``run-cadence.ts`` rule exactly, so a
schedule imported from either collection keeps firing at the same wall-clock
instants: the EARLIEST instant at which ``time_zone`` shows the requested wall
clock; and when it never shows it (spring forward), the requested time shifted
LATER by the gap. Never earlier -- a post that goes out an hour early is out at
a time the client did not pick and cannot take back.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from app.core.exceptions import InvalidStateError, ResourceNotFoundError, ValidationRefusedError
from app.db.postgres import ConfigDatabase

logger = logging.getLogger(__name__)

CADENCES = ("daily", "weekly", "monthly")
STATUSES = ("active", "paused", "completed")

#: How long a fire may sit in flight before a later claim treats it as
#: vanished. A container recycle between "advance the cursor" and "submit the
#: job" leaves the row in flight forever; without this the schedule is dead
#: and looks healthy. Generous, because a slow submit is not a vanished one.
IN_FLIGHT_GRACE = timedelta(minutes=30)

#: A stored refusal is one readable sentence on the schedule row, not a log.
MAX_ERROR_CHARS = 400


def _refuse(message: str, field: str) -> ValidationRefusedError:
    """One problem, in the shape the 422 handler renders for publish refusals."""

    return ValidationRefusedError(message, [{"field": field, "problem": message}])


# --- Cadence -----------------------------------------------------------------


def _wall_clock_in_zone(
    year: int, month: int, day: int, hour: int, minute: int, zone: ZoneInfo
) -> datetime:
    """The rule from the module docstring, as one function.

    ``fold=0`` is the earlier of two ambiguous instants. A nonexistent wall
    clock is detected the standard way -- the round trip through UTC does not
    land back on the requested wall clock -- and shifted later by the gap.
    """

    naive = datetime(year, month, day, hour, minute)
    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) == naive:
        return candidate
    # Nonexistent: the zone skipped this wall clock. `candidate.utcoffset()` is
    # the offset BEFORE the gap, so `astimezone(UTC)` landed `gap` later on the
    # far side; that later instant is the one the rule asks for.
    return round_trip


def _clamp_day(year: int, month: int, day: int) -> int:
    """``day_of_month`` 31 in a 30-day month fires on the 30th, like the portal."""

    next_month = datetime(year + (month == 12), (month % 12) + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return min(day, last_day)


def next_fire_at(
    *,
    cadence: str,
    hour: int,
    minute: int,
    time_zone: str,
    after: datetime,
    weekdays: list[int] | None = None,
    day_of_month: int | None = None,
) -> datetime:
    """The first instant strictly after ``after`` at which this schedule fires.

    ``weekdays`` uses the portal's convention, 0=Sunday .. 6=Saturday, because
    every imported row is written in it and a silent re-basing to Python's
    Monday=0 is the kind of off-by-one that fires a Monday post on Tuesday.
    """

    try:
        zone = ZoneInfo(time_zone)
    except ZoneInfoNotFoundError as exc:
        raise _refuse(f"time_zone {time_zone!r} is not an IANA zone", "time_zone") from exc

    local_after = after.astimezone(zone)
    # Walk forward day by day from today-in-zone; the first candidate strictly
    # after `after` wins. Bounded: 366 days covers every cadence, including a
    # day_of_month=31 schedule created in February.
    start = local_after.date()
    for offset in range(0, 366):
        day = start + timedelta(days=offset)
        if cadence == "weekly":
            portal_weekday = (day.weekday() + 1) % 7  # Python Mon=0 -> portal Sun=0
            if weekdays is None or portal_weekday not in weekdays:
                continue
            fire_day = day.day
        elif cadence == "monthly":
            if day_of_month is None:
                raise _refuse("a monthly schedule needs day_of_month", "day_of_month")
            fire_day = _clamp_day(day.year, day.month, day_of_month)
            if day.day != fire_day:
                continue
        elif cadence == "daily":
            fire_day = day.day
        else:
            raise _refuse(f"cadence {cadence!r} is not one of {CADENCES}", "cadence")

        candidate = _wall_clock_in_zone(day.year, day.month, fire_day, hour, minute, zone)
        if candidate > after:
            return candidate.astimezone(UTC)

    raise _refuse("no fire instant within a year -- the cadence names no day", "cadence")


# --- The service -------------------------------------------------------------


class ScheduleService:
    """Definitions, the claim/settle protocol, and nothing about money."""

    def __init__(self, db: ConfigDatabase) -> None:
        self._db = db

    # --- Definitions --------------------------------------------------------

    async def create(
        self,
        *,
        client_slug: str,
        agent_slug: str,
        label: str,
        prompt: str,
        cadence: str,
        hour: int,
        minute: int,
        time_zone: str,
        bill_client_credits: bool,
        weekdays: list[int] | None = None,
        day_of_month: int | None = None,
        outputs_per_run: int = 1,
        created_by: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """A new schedule, active, with its first fire computed.

        ``bill_client_credits`` has no default here either. A caller that does
        not know whether the client pays for this schedule does not get to
        create it; that question being unanswered is how ``charge: null`` came
        to be.
        """

        first = next_fire_at(
            cadence=cadence, hour=hour, minute=minute, time_zone=time_zone,
            after=now or datetime.now(UTC), weekdays=weekdays, day_of_month=day_of_month,
        )
        try:
            row = await self._db.fetchrow(
                """
                insert into schedules (
                    client_slug, agent_slug, label, prompt, cadence, hour, minute,
                    weekdays, day_of_month, time_zone, outputs_per_run,
                    bill_client_credits, billing_intent_source, next_run_at, created_by
                ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'explicit', $13, $14)
                returning *
                """,
                client_slug, agent_slug, label, prompt, cadence, hour, minute,
                weekdays, day_of_month, time_zone, outputs_per_run,
                bill_client_credits, first, created_by,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise ResourceNotFoundError("agent", agent_slug) from exc
        except asyncpg.CheckViolationError as exc:
            raise _refuse(_check_message(exc), "schedule") from exc
        assert row is not None
        return _row(row)

    async def get(self, schedule_id: str) -> dict[str, Any]:
        row = await self._db.fetchrow("select * from schedules where id = $1", _uuid(schedule_id))
        if row is None:
            raise ResourceNotFoundError("schedule", schedule_id)
        return _row(row)

    async def list_for_client(
        self, client_slug: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], bool]:
        rows = await self._db.fetch(
            """
            select * from schedules
             where client_slug = $1 and ($2::text is null or status = $2)
             order by next_run_at, id
             limit $3 offset $4
            """,
            client_slug, status, limit + 1, offset,
        )
        return [_row(r) for r in rows[:limit]], len(rows) > limit

    async def set_status(
        self, schedule_id: str, status: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Pause, resume or complete.

        Resuming recomputes the cursor from now: a schedule paused for a month
        must not fire thirty times to catch up. Pausing clears nothing -- a fire
        in flight settles on its own.
        """

        if status not in STATUSES:
            raise _refuse(f"status {status!r} is not one of {STATUSES}", "status")
        current = await self.get(schedule_id)
        if current["status"] == status:
            return current
        next_run_at = current["next_run_at"]
        if status == "active":
            next_run_at = next_fire_at(
                cadence=current["cadence"], hour=current["hour"], minute=current["minute"],
                time_zone=current["time_zone"], after=now or datetime.now(UTC),
                weekdays=current["weekdays"], day_of_month=current["day_of_month"],
            )
        row = await self._db.fetchrow(
            "update schedules set status = $2, next_run_at = $3 where id = $1 returning *",
            _uuid(schedule_id), status, next_run_at,
        )
        assert row is not None
        return _row(row)

    # --- The fire protocol -----------------------------------------------------

    async def claim_due(
        self, *, now: datetime | None = None, limit: int = 25
    ) -> tuple[str, list[dict[str, Any]]]:
        """Claim every active schedule whose cursor has passed, up to ``limit``.

        One transaction. ``FOR UPDATE SKIP LOCKED`` means a second tick running
        at the same instant claims a disjoint set rather than the same rows a
        moment later; the cursor is advanced and ``fire_in_flight_since`` is
        stamped in the same statement that returns the row, so there is no
        window in which a row is due, unlocked and unclaimed.

        Returns the claim id and the claimed rows. Each row carries
        ``bill_client_credits``; the executor gets a billing decision or it
        gets nothing.

        A row still in flight from a claim older than ``IN_FLIGHT_GRACE`` is a
        fire that vanished -- a container recycle between claim and submit. It
        is re-claimed here with ``last_error`` recording the vanished claim, so
        the schedule recovers on its own and the loss is visible on the row.
        """

        at = now or datetime.now(UTC)
        claim_id = uuid.uuid4()
        claimed: list[dict[str, Any]] = []
        async with self._db.transaction() as connection:
            due = await connection.fetch(
                """
                select * from schedules
                 where status = 'active'
                   and next_run_at <= $1
                   and (fire_in_flight_since is null or fire_in_flight_since <= $2)
                 order by next_run_at, id
                 limit $3
                 for update skip locked
                """,
                at, at - IN_FLIGHT_GRACE, limit,
            )
            for row in due:
                stale_since = row["fire_in_flight_since"]
                vanished = stale_since is not None
                advanced = next_fire_at(
                    cadence=row["cadence"], hour=row["hour"], minute=row["minute"],
                    time_zone=row["time_zone"], after=at,
                    weekdays=row["weekdays"], day_of_month=row["day_of_month"],
                )
                updated = await connection.fetchrow(
                    """
                    update schedules
                       set next_run_at = $2,
                           last_run_at = $3,
                           fire_in_flight_since = $3,
                           fire_claim_id = $4,
                           last_error = case when $5::boolean then $6::text else last_error end,
                           last_error_at = case when $5::boolean then $3::timestamptz
                                                else last_error_at end
                     where id = $1
                 returning *
                    """,
                    row["id"], advanced, at, claim_id,
                    vanished,
                    (
                        f"fire claimed {stale_since.isoformat()} never settled; re-claimed"
                        if stale_since is not None
                        else None
                    ),
                )
                assert updated is not None
                fired_for = row["next_run_at"]
                claimed.append(
                    {**_row(updated), "fired_for": fired_for, "vanished_claim": vanished}
                )
                if stale_since is not None:
                    logger.warning(
                        "schedule %s: fire claimed at %s never settled; re-claimed as %s",
                        row["id"], stale_since.isoformat(), claim_id,
                    )
        return str(claim_id), claimed

    async def settle(
        self,
        schedule_id: str,
        *,
        claim_id: str,
        job_id: str | None = None,
        error: str | None = None,
        disable: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record how a claimed fire ended and clear the in-flight marker.

        Only the claim that took the row may settle it: a settle carrying a
        stale claim id (the tick that vanished, waking up late) is refused,
        because the row has moved on and its report is about a fire that no
        longer exists.

        ``error`` is stored on the row rather than only logged: a submit
        refused before a job exists leaves no other trace, and the cursor has
        already advanced. ``disable`` is for the case the portal handles today
        -- agent gone or client gone -- where firing again next tick helps
        nobody.
        """

        at = now or datetime.now(UTC)
        row = await self._db.fetchrow(
            """
            update schedules
               set fire_in_flight_since = null,
                   fire_claim_id = null,
                   last_job_id = coalesce($3::text, last_job_id),
                   last_error = $4::text,
                   last_error_at = case when $4::text is null then null else $5::timestamptz end,
                   status = case when $6::boolean then 'paused' else status end
             where id = $1 and fire_claim_id = $2
         returning *
            """,
            _uuid(schedule_id), _uuid(claim_id), job_id,
            error[:MAX_ERROR_CHARS] if error else None, at, disable,
        )
        if row is None:
            current = await self._db.fetchrow(
                "select fire_claim_id from schedules where id = $1", _uuid(schedule_id)
            )
            if current is None:
                raise ResourceNotFoundError("schedule", schedule_id)
            raise InvalidStateError(
                f"schedule {schedule_id} is not in flight under claim {claim_id}"
                + (
                    f" (current claim: {current['fire_claim_id']})"
                    if current["fire_claim_id"] else " (nothing in flight)"
                )
            )
        return _row(row)

    async def in_flight(self, *, older_than: timedelta | None = None) -> list[dict[str, Any]]:
        """Rows with a fire in flight -- all of them, or only the suspicious ones."""

        cutoff = datetime.now(UTC) - (older_than or timedelta(0))
        rows = await self._db.fetch(
            "select * from schedules where fire_in_flight_since is not null "
            "and fire_in_flight_since <= $1 order by fire_in_flight_since",
            cutoff,
        )
        return [_row(r) for r in rows]


# --- Row shaping ---------------------------------------------------------------


def _row(row: asyncpg.Record) -> dict[str, Any]:
    out = dict(row)
    out["id"] = str(out["id"])
    if out.get("fire_claim_id") is not None:
        out["fire_claim_id"] = str(out["fire_claim_id"])
    if out.get("weekdays") is not None:
        out["weekdays"] = list(out["weekdays"])
    return out


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ResourceNotFoundError("schedule", str(value)) from exc


def _check_message(exc: asyncpg.CheckViolationError) -> str:
    name = getattr(exc, "constraint_name", None) or "a schema constraint"
    return f"schedule refused by the database: {name}"
