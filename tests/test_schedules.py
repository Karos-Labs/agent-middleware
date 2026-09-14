"""Schedules on Postgres: the middleware half of S10 / SCRUM-223.

Two systems in the portal, one of which bills nobody. What these pin, against
a real PostgreSQL 16:

    * A schedule cannot be created without saying who pays, and a fire cannot
      be claimed without the answer coming back with it. That is the defect
      (`charge: null`, unconditionally) closed at the type level.
    * The claim is `SELECT ... FOR UPDATE SKIP LOCKED`: two ticks at the same
      instant get disjoint sets, and a claimed row is advanced and marked in
      flight in the statement that returns it.
    * Only the claim that took a row may settle it. A fire that vanished is
      re-claimed after the grace period with the loss recorded on the row.
    * The cadence math is the portal's: earliest instant for an ambiguous
      wall clock, later-by-the-gap for a nonexistent one, day 31 clamped,
      weekdays with Sunday = 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.firestore import FirestoreDB
from app.db.postgres import ConfigDatabase
from app.main import build_services, create_app
from app.services.schedules import IN_FLIGHT_GRACE, ScheduleService, next_fire_at
from tests.conftest_postgres import requires_postgres

pytestmark = requires_postgres

NY = "America/New_York"
IL = "Asia/Jerusalem"


# --- Cadence math (pure) ---------------------------------------------------------


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_daily_fires_at_the_next_wall_clock_in_zone() -> None:
    # 09:00 Jerusalem on 2026-06-10 is 06:00Z. Asked at 05:00Z -> today; at 07:00Z -> tomorrow.
    assert next_fire_at(
        cadence="daily", hour=9, minute=0, time_zone=IL, after=at("2026-06-10T05:00:00+00:00")
    ) == at("2026-06-10T06:00:00+00:00")
    assert next_fire_at(
        cadence="daily", hour=9, minute=0, time_zone=IL, after=at("2026-06-10T07:00:00+00:00")
    ) == at("2026-06-11T06:00:00+00:00")


def test_the_boundary_is_strictly_after() -> None:
    exact = at("2026-06-10T06:00:00+00:00")
    assert next_fire_at(cadence="daily", hour=9, minute=0, time_zone=IL, after=exact) == at(
        "2026-06-11T06:00:00+00:00"
    )


def test_weekly_uses_the_portals_sunday_zero_convention() -> None:
    # 2026-06-10 is a Wednesday. Weekdays [0, 3] = Sunday and Wednesday.
    wednesday_after = at("2026-06-10T12:00:00+00:00")  # past 09:00 Jerusalem
    fired = next_fire_at(
        cadence="weekly", hour=9, minute=0, time_zone=IL, after=wednesday_after, weekdays=[0, 3]
    )
    local = fired.astimezone(ZoneInfo(IL))
    assert (local.year, local.month, local.day, local.hour) == (2026, 6, 14, 9)  # Sunday
    assert local.weekday() == 6  # Python's Sunday, i.e. the portal's 0


def test_monthly_clamps_day_31_to_the_months_length() -> None:
    fired = next_fire_at(
        cadence="monthly", hour=8, minute=30, time_zone=IL,
        after=at("2026-02-01T00:00:00+00:00"), day_of_month=31,
    )
    local = fired.astimezone(ZoneInfo(IL))
    assert (local.month, local.day, local.hour, local.minute) == (2, 28, 8, 30)


def test_a_nonexistent_wall_clock_shifts_later_by_the_gap() -> None:
    # 2026-03-08: New York springs forward at 02:00 -> 03:00. 02:30 never happens.
    fired = next_fire_at(
        cadence="daily", hour=2, minute=30, time_zone=NY, after=at("2026-03-08T00:00:00-05:00")
    )
    local = fired.astimezone(ZoneInfo(NY))
    # Later, never earlier: 03:30 EDT, which is 07:30Z. 01:30 EST would be 06:30Z.
    assert (local.hour, local.minute) == (3, 30)
    assert fired == at("2026-03-08T07:30:00+00:00")


def test_an_ambiguous_wall_clock_fires_once_at_the_earlier_instant() -> None:
    # 2026-11-01: New York falls back at 02:00 -> 01:00. 01:30 happens twice.
    fired = next_fire_at(
        cadence="daily", hour=1, minute=30, time_zone=NY, after=at("2026-11-01T00:00:00-04:00")
    )
    assert fired == at("2026-11-01T05:30:00+00:00")  # 01:30 EDT, the first one
    # And the NEXT fire is the next day's, not the second 01:30 an hour later.
    following = next_fire_at(cadence="daily", hour=1, minute=30, time_zone=NY, after=fired)
    assert following == at("2026-11-02T06:30:00+00:00")  # 01:30 EST


def test_an_unknown_zone_is_refused_not_guessed() -> None:
    from app.core.exceptions import ValidationRefusedError

    with pytest.raises(ValidationRefusedError, match="IANA"):
        next_fire_at(
            cadence="daily", hour=9, minute=0, time_zone="Mars/Olympus", after=datetime.now(UTC)
        )


# --- Fixtures -------------------------------------------------------------------


@pytest.fixture
async def api(
    settings: Any, database: FirestoreDB, publisher_service: Any, config_database: ConfigDatabase
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app, settings, database, publisher=publisher_service, config_database=config_database
        )
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client


@pytest.fixture
async def agent_slug(config_database: ConfigDatabase) -> str:
    await config_database.execute(
        """
        insert into agents (slug, name, agent_class_code, capabilities, platforms)
        values ('x-agent', 'X Agent', 'drafting', array['draft_social_post'], array['x'])
        """
    )
    return "x-agent"


@pytest.fixture
def service(config_database: ConfigDatabase) -> ScheduleService:
    return ScheduleService(config_database)


def body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "agent_slug": "x-agent",
        "label": "Weekly X post",
        "prompt": "Draft the next company-page post.",
        "cadence": "daily",
        "hour": 9,
        "minute": 0,
        "time_zone": IL,
        "bill_client_credits": True,
    }
    base.update(over)
    return base


# --- Definitions: who pays is stated, never inferred ----------------------------


async def test_a_schedule_without_a_billing_decision_is_not_created(
    api: AsyncClient, agent_slug: str
) -> None:
    payload = body()
    del payload["bill_client_credits"]
    response = await api.post("/clients/acme/schedules", json=payload)
    assert response.status_code == 422, response.text
    assert "bill_client_credits" in response.text


async def test_create_computes_the_first_fire_and_records_the_decision_as_explicit(
    api: AsyncClient, agent_slug: str
) -> None:
    response = await api.post("/clients/acme/schedules", json=body(bill_client_credits=False))
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["bill_client_credits"] is False
    assert created["billing_intent_source"] == "explicit"
    assert created["status"] == "active"
    assert created["fire_in_flight_since"] is None
    next_run = datetime.fromisoformat(created["next_run_at"])
    assert next_run > datetime.now(UTC)
    assert next_run.astimezone(ZoneInfo(IL)).hour == 9

    listed = (await api.get("/clients/acme/schedules")).json()
    assert [s["id"] for s in listed["items"]] == [created["id"]]
    assert (await api.get(f"/schedules/{created['id']}")).json()["label"] == "Weekly X post"


async def test_cadence_fields_must_agree_before_the_database_is_asked(
    api: AsyncClient, agent_slug: str
) -> None:
    weekly_without_days = await api.post(
        "/clients/acme/schedules", json=body(cadence="weekly")
    )
    assert weekly_without_days.status_code == 422
    daily_with_day = await api.post(
        "/clients/acme/schedules", json=body(cadence="daily", day_of_month=3)
    )
    assert daily_with_day.status_code == 422


async def test_an_unknown_agent_is_404(api: AsyncClient, agent_slug: str) -> None:
    response = await api.post("/clients/acme/schedules", json=body(agent_slug="nope"))
    assert response.status_code == 404, response.text


async def test_pause_holds_the_cursor_and_resume_recomputes_it_from_now(
    api: AsyncClient, agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    created = (await api.post("/clients/acme/schedules", json=body())).json()
    # Pretend the schedule sat paused for a month by pushing its cursor into the past.
    long_ago = datetime.now(UTC) - timedelta(days=30)
    await config_database.execute(
        "update schedules set next_run_at = $2 where id = $1", created["id"], long_ago
    )

    paused = (
        await api.post(f"/schedules/{created['id']}/status", json={"status": "paused"})
    ).json()
    assert paused["status"] == "paused"
    assert datetime.fromisoformat(paused["next_run_at"]) == long_ago  # held, not touched

    # A paused schedule is never claimed, however overdue.
    _, fires = await service.claim_due()
    assert fires == []

    resumed = (
        await api.post(f"/schedules/{created['id']}/status", json={"status": "active"})
    ).json()
    assert resumed["status"] == "active"
    assert datetime.fromisoformat(resumed["next_run_at"]) > datetime.now(UTC)  # no 30-fire replay


# --- The fire protocol -------------------------------------------------------------


async def make_due(
    service: ScheduleService, config_database: ConfigDatabase, *, label: str, bill: bool
) -> dict[str, Any]:
    created = await service.create(
        client_slug="acme", agent_slug="x-agent", label=label, prompt="go", cadence="daily",
        hour=9, minute=0, time_zone=IL, bill_client_credits=bill,
    )
    due_at = datetime.now(UTC) - timedelta(minutes=5)
    await config_database.execute(
        "update schedules set next_run_at = $2 where id = $1", created["id"], due_at
    )
    return {**created, "next_run_at": due_at}


async def test_claim_returns_every_due_fire_with_its_billing_decision(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    billed = await make_due(service, config_database, label="billed", bill=True)
    free = await make_due(service, config_database, label="free", bill=False)
    not_due = await service.create(
        client_slug="acme", agent_slug="x-agent", label="later", prompt="", cadence="daily",
        hour=9, minute=0, time_zone=IL, bill_client_credits=True,
    )

    now = datetime.now(UTC)
    claim_id, fires = await service.claim_due(now=now)

    assert {f["id"] for f in fires} == {billed["id"], free["id"]}
    assert not_due["id"] not in {f["id"] for f in fires}
    by_label = {f["label"]: f for f in fires}
    # The whole point: the executor receives the decision with the fire.
    assert by_label["billed"]["bill_client_credits"] is True
    assert by_label["free"]["bill_client_credits"] is False
    for fire in fires:
        assert fire["fire_claim_id"] == claim_id
        assert fire["fire_in_flight_since"] == now
        assert fire["last_run_at"] == now
        assert fire["fired_for"] == billed["next_run_at"] if fire["id"] == billed["id"] else True
        assert fire["next_run_at"] > now  # the cursor advanced in the same statement
        assert fire["vanished_claim"] is False

    # Claimed rows are in flight and not due again: a second tick gets nothing.
    _, again = await service.claim_due(now=now + timedelta(seconds=1))
    assert again == []


async def test_concurrent_claims_get_disjoint_sets(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    """SKIP LOCKED, observed. Two ticks at once split the due rows, never share one."""

    ids = {
        (await make_due(service, config_database, label=f"s{i}", bill=bool(i % 2)))["id"]
        for i in range(6)
    }
    now = datetime.now(UTC)
    (claim_a, fires_a), (claim_b, fires_b) = await asyncio.gather(
        service.claim_due(now=now, limit=3), service.claim_due(now=now, limit=3)
    )
    got_a = {f["id"] for f in fires_a}
    got_b = {f["id"] for f in fires_b}
    assert claim_a != claim_b
    assert got_a.isdisjoint(got_b)
    assert got_a | got_b == ids


async def test_settle_with_a_job_clears_the_flight_and_records_the_job(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    due = await make_due(service, config_database, label="s", bill=True)
    claim_id, fires = await service.claim_due()
    assert [f["id"] for f in fires] == [due["id"]]

    settled = await service.settle(due["id"], claim_id=claim_id, job_id="job-1")
    assert settled["fire_in_flight_since"] is None
    assert settled["fire_claim_id"] is None
    assert settled["last_job_id"] == "job-1"
    assert settled["last_error"] is None
    assert settled["status"] == "active"
    assert await service.in_flight() == []


async def test_settle_with_an_error_records_it_on_the_row_and_can_disable(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    due = await make_due(service, config_database, label="s", bill=True)
    claim_id, _ = await service.claim_due()

    refused = await service.settle(
        due["id"], claim_id=claim_id, error="Client is out of credits " + "x" * 500
    )
    assert refused["last_error"] is not None and len(refused["last_error"]) <= 400
    assert refused["last_error_at"] is not None
    assert refused["status"] == "active"  # refused this time; fires again next slot

    # Next slot: the agent is gone. Settle with disable so it stops churning.
    await config_database.execute(
        "update schedules set next_run_at = $2 where id = $1",
        due["id"], datetime.now(UTC) - timedelta(minutes=1),
    )
    claim_id, fires = await service.claim_due()
    assert [f["id"] for f in fires] == [due["id"]]
    gone = await service.settle(
        due["id"], claim_id=claim_id, error="Agent missing or disabled", disable=True
    )
    assert gone["status"] == "paused"
    assert gone["fire_in_flight_since"] is None


async def test_only_the_claim_that_took_the_row_may_settle_it(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    from app.core.exceptions import InvalidStateError

    due = await make_due(service, config_database, label="s", bill=True)
    claim_id, _ = await service.claim_due()

    stale = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(InvalidStateError, match="not in flight under claim"):
        await service.settle(due["id"], claim_id=stale, job_id="job-x")

    # The real claim still can.
    await service.settle(due["id"], claim_id=claim_id, job_id="job-1")
    # And settling twice is refused: nothing is in flight any more.
    with pytest.raises(InvalidStateError, match="nothing in flight"):
        await service.settle(due["id"], claim_id=claim_id, job_id="job-1")


async def test_a_vanished_fire_is_reclaimed_after_the_grace_period_with_the_loss_recorded(
    agent_slug: str, service: ScheduleService, config_database: ConfigDatabase
) -> None:
    """A container recycle between claim and submit leaves the row in flight forever.

    Within the grace period the row is left alone (a slow submit is not a lost
    one). After it, the next tick takes the row back, and `last_error` says
    which claim never came home, so the loss is on the row and not in a log
    nobody reads.
    """

    due = await make_due(service, config_database, label="s", bill=True)
    first_claim, _ = await service.claim_due(now=datetime.now(UTC))
    # Make it due again while still in flight.
    await config_database.execute(
        "update schedules set next_run_at = $2 where id = $1",
        due["id"], datetime.now(UTC) - timedelta(minutes=1),
    )

    within_grace = datetime.now(UTC) + IN_FLIGHT_GRACE - timedelta(minutes=1)
    _, untouched = await service.claim_due(now=within_grace)
    assert untouched == []
    assert [s["id"] for s in await service.in_flight()] == [due["id"]]

    after_grace = datetime.now(UTC) + IN_FLIGHT_GRACE + timedelta(minutes=1)
    second_claim, fires = await service.claim_due(now=after_grace)
    assert [f["id"] for f in fires] == [due["id"]]
    assert fires[0]["vanished_claim"] is True
    assert fires[0]["fire_claim_id"] == second_claim != first_claim
    assert "never settled" in (fires[0]["last_error"] or "")

    # The late tick from the vanished claim cannot settle over the new one.
    from app.core.exceptions import InvalidStateError

    with pytest.raises(InvalidStateError):
        await service.settle(due["id"], claim_id=first_claim, job_id="ghost")


# --- Over HTTP, as the executor will call it -----------------------------------------


async def test_the_executor_protocol_over_http(
    api: AsyncClient, agent_slug: str, config_database: ConfigDatabase
) -> None:
    created = (
        await api.post("/clients/acme/schedules", json=body(bill_client_credits=False))
    ).json()
    await config_database.execute(
        "update schedules set next_run_at = $2 where id = $1",
        created["id"], datetime.now(UTC) - timedelta(minutes=1),
    )

    claimed = await api.post("/schedules/claim", json={"limit": 10})
    assert claimed.status_code == 200, claimed.text
    payload = claimed.json()
    assert [f["id"] for f in payload["fires"]] == [created["id"]]
    assert payload["fires"][0]["bill_client_credits"] is False
    assert payload["fires"][0]["fired_for"]

    in_flight = await api.get("/schedules/in-flight")
    assert [s["id"] for s in in_flight.json()] == [created["id"]]

    both = await api.post(
        f"/schedules/{created['id']}/settle",
        json={"claim_id": payload["claim_id"], "job_id": "j", "error": "e"},
    )
    assert both.status_code == 422  # one outcome, not two

    settled = await api.post(
        f"/schedules/{created['id']}/settle",
        json={"claim_id": payload["claim_id"], "job_id": "job-1"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["last_job_id"] == "job-1"
    assert (await api.get("/schedules/in-flight")).json() == []

    stale = await api.post(
        f"/schedules/{created['id']}/settle",
        json={"claim_id": payload["claim_id"], "job_id": "job-1"},
    )
    assert stale.status_code == 409


async def test_the_routes_report_themselves_unavailable_without_a_dsn(
    settings: Any, database: FirestoreDB, publisher_service: Any
) -> None:
    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(app, settings, database, publisher=publisher_service, config_database=None)
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/clients/acme/schedules")
    assert response.status_code == 503
    assert "CONFIG_DB_DSN" in response.json()["detail"]


# --- Bringing the two collections over --------------------------------------------


async def test_the_two_portal_collections_import_with_money_stated_never_guessed(
    api: AsyncClient, database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    """S5's shape for S10's data, and the one rule that matters: who pays is read, not inferred.

    Six documents. Three import, three are refused, each for a reason a person
    has to answer -- and the dry run promises exactly that before writing.
    """

    from app.services.schedule_import import ScheduleImporter

    # The bridges: a client with a workspace slug, and the two customAgents S5
    # brought into config.agents with their Firestore ids as source_id.
    await database.document("clients", "client-1").set({"name": "Acme", "agentsRepoSlug": "acme"})
    await database.document("clients", "client-2").set({"name": "No slug yet"})
    for slug, source in (("x-agent", "ca-x"), ("li-agent", "ca-li")):
        await config_database.execute(
            """
            insert into agents (slug, name, agent_class_code, capabilities, platforms,
                                source_registry, source_id, imported_at)
            values ($1, $2, 'drafting', array['draft_social_post'], array['x'],
                    'customAgents', $3, now())
            """,
            slug, slug, source,
        )

    # scheduledRuns: the unbilled system. Imported as false / inferred_at_import.
    await database.document("scheduledRuns", "sr-1").set({
        "clientId": "client-1", "agentId": "ca-x", "label": "Legacy weekly", "prompt": "go",
        "cadence": {"daysOfWeek": [1, 4], "hour": 10, "minute": 15, "timezone": IL},
        "enabled": True, "nextRunAt": 1_800_000_000_000, "lastJobId": "job-9",
    })
    # plannedScheduledRuns with the money switch stated: explicit.
    await database.document("plannedScheduledRuns", "pr-1").set({
        "clientId": "client-1", "customAgentId": "ca-li", "agentName": "LinkedIn weekly",
        "prompt": "post", "cadence": "weekly", "hour": 9, "minute": 0, "weekday": 2,
        "timeZone": IL, "billClientCredits": True, "status": "active",
        "nextRunAt": 1_800_000_000_000, "outputsPerRun": 3,
    })
    # plannedScheduledRuns, monthly, paused, no zone -> only with --assume-time-zone.
    await database.document("plannedScheduledRuns", "pr-2").set({
        "clientId": "client-1", "customAgentId": "ca-li", "agentName": "Monthly",
        "cadence": "monthly", "hour": 8, "minute": 0, "dayOfMonth": 31,
        "billClientCredits": False, "status": "paused",
    })
    # Refused: no billing decision on the record.
    await database.document("plannedScheduledRuns", "pr-3").set({
        "clientId": "client-1", "customAgentId": "ca-li", "agentName": "Undecided",
        "cadence": "daily", "hour": 7, "minute": 0, "timeZone": IL, "status": "active",
    })
    # Refused: a one-off is not a schedule.
    await database.document("plannedScheduledRuns", "pr-4").set({
        "clientId": "client-1", "customAgentId": "ca-li", "agentName": "One-off",
        "cadence": "once", "hour": 7, "minute": 0, "timeZone": IL, "billClientCredits": True,
    })
    # Refused: the client has no workspace slug to map to.
    await database.document("scheduledRuns", "sr-2").set({
        "clientId": "client-2", "agentId": "ca-x", "label": "Orphan",
        "cadence": {"daysOfWeek": [0], "hour": 10, "minute": 0, "timezone": IL},
    })

    importer = ScheduleImporter(database, config_database, assume_time_zone=IL)

    dry = await importer.run(actor="test", dry_run=True)
    assert dry.counts() == {"imported": 3, "refused": 3}
    assert await config_database.fetchval("select count(*) from schedules") == 0

    first = await importer.run(actor="test")
    assert first.counts() == {"imported": 3, "refused": 3}
    second = await importer.run(actor="test")
    assert second.counts() == {"already_present": 3, "refused": 3}

    reasons = {o.source_id: " ".join(o.reasons) for o in first.refused}
    assert "billClientCredits is absent" in reasons["pr-3"]
    assert "'once'" in reasons["pr-4"]
    assert "agentsRepoSlug" in reasons["sr-2"]

    rows = {
        r["source_id"]: r
        for r in await config_database.fetch(
            "select * from schedules order by source_system, source_id"
        )
    }
    legacy = rows["sr-1"]
    assert (legacy["client_slug"], legacy["agent_slug"], legacy["cadence"]) == (
        "acme", "x-agent", "weekly",
    )
    assert list(legacy["weekdays"]) == [1, 4]
    assert (legacy["bill_client_credits"], legacy["billing_intent_source"]) == (
        False, "inferred_at_import",
    )
    assert legacy["next_run_at"] == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert legacy["last_job_id"] == "job-9"

    planned = rows["pr-1"]
    assert (planned["bill_client_credits"], planned["billing_intent_source"]) == (True, "explicit")
    assert list(planned["weekdays"]) == [2]  # the single `weekday` became the list
    assert planned["outputs_per_run"] == 3
    assert planned["label"] == "LinkedIn weekly"

    monthly = rows["pr-2"]
    assert (monthly["cadence"], monthly["day_of_month"], monthly["status"]) == (
        "monthly", 31, "paused",
    )
    assert monthly["time_zone"] == IL  # the assumed zone, stated on the command line
    assert monthly["next_run_at"] > datetime.now(UTC)  # no cursor on the source; computed

    # The imported rows are the ones the API now serves, unbilled ones visibly so.
    listed = (await api.get("/clients/acme/schedules")).json()
    unbilled = [s for s in listed["items"] if not s["bill_client_credits"]]
    assert {s["source_id"] for s in unbilled} == {"sr-1", "pr-2"}
    assert {s["billing_intent_source"] for s in unbilled} == {"inferred_at_import", "explicit"}


async def test_without_an_assumed_zone_a_zoneless_row_is_refused(
    database: FirestoreDB, config_database: ConfigDatabase
) -> None:
    from app.services.schedule_import import ScheduleImporter

    await database.document("clients", "c").set({"agentsRepoSlug": "acme"})
    await config_database.execute(
        """
        insert into agents (slug, name, agent_class_code, capabilities, platforms,
                            source_registry, source_id, imported_at)
        values ('x-agent', 'X', 'drafting', array['draft_social_post'], array['x'],
                'customAgents', 'ca', now())
        """
    )
    await database.document("plannedScheduledRuns", "p").set({
        "clientId": "c", "customAgentId": "ca", "cadence": "daily", "hour": 9, "minute": 0,
        "billClientCredits": True,
    })
    report = await ScheduleImporter(database, config_database).run(actor="test")
    assert report.counts() == {"refused": 1}
    assert "timeZone is absent" in report.refused[0].reasons[0]
