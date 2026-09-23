"""Readiness has to say something about the OTHER store.

Production ran with **zero tables** in `karos-prod` — not one migration had
ever been applied — while `/health/ready` answered
`{"status":"ok","firestore_reachable":true}` and the prod deploy's "Verify the
new revision is serving" step passed every time. Nothing was lying. The service
really was serving and thirty of its routes really were fine; readiness simply
never asked about Cloud SQL, so "is this healthy" had no way to return the one
answer that mattered.

The four states are not degrees of one thing and do not share a fix, which is
why they are four words and not a boolean.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.routes.health import (
    CONFIG_DB_NOT_CONFIGURED,
    CONFIG_DB_OK,
    CONFIG_DB_SCHEMA_MISSING,
    CONFIG_DB_UNAVAILABLE,
    config_database_state,
)


class _Database:
    def __init__(self, applied: bool | Exception) -> None:
        self._applied = applied

    async def schema_is_applied(self) -> bool:
        if isinstance(self._applied, Exception):
            raise self._applied
        return self._applied


class _Settings:
    def __init__(self, dsn: str | None) -> None:
        self.config_db_dsn = dsn


def _request(*, config_database: object | None, dsn: str | None) -> Request:
    app = FastAPI()
    app.state.config_database = config_database
    app.state.settings = _Settings(dsn)
    return Request({"type": "http", "app": app, "headers": []})


@pytest.mark.asyncio
async def test_a_migrated_database_reads_ok() -> None:
    state = await config_database_state(_request(config_database=_Database(True), dsn="postgresql://x"))
    assert state == CONFIG_DB_OK


@pytest.mark.asyncio
async def test_an_empty_database_is_schema_missing_not_ok() -> None:
    # The production state. The pool builds, the connection works, and there is
    # not one table behind it.
    state = await config_database_state(_request(config_database=_Database(False), dsn="postgresql://x"))
    assert state == CONFIG_DB_SCHEMA_MISSING


@pytest.mark.asyncio
async def test_configured_but_no_pool_is_unavailable_not_not_configured() -> None:
    # The distinction is the whole point: this one is an instance, a socket or
    # a grant. `not_configured` would send someone to look at the wrong thing.
    state = await config_database_state(_request(config_database=None, dsn="postgresql://x"))
    assert state == CONFIG_DB_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_environment_with_no_cloud_sql_is_not_a_fault() -> None:
    # Cloud SQL does not exist in every environment, and a control plane that
    # called that a fault would make the Postgres migration a flag day.
    state = await config_database_state(_request(config_database=None, dsn=None))
    assert state == CONFIG_DB_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_a_probe_never_raises() -> None:
    # A readiness probe that 500s is one nobody can build a deploy check on.
    state = await config_database_state(
        _request(config_database=_Database(RuntimeError("connection reset")), dsn="postgresql://x")
    )
    assert state == CONFIG_DB_UNAVAILABLE


def test_readiness_reports_the_field(client: TestClient) -> None:
    body = client.get("/health/ready").json()
    assert "config_database" in body


def test_an_empty_database_still_serves_and_still_says_so(client: TestClient) -> None:
    # Deliberate asymmetry, and the reason this is a field rather than a
    # verdict. `build_config_database` degrades instead of refusing to start so
    # the thirty Firestore-only routes keep serving when Cloud SQL is missing.
    # Failing readiness on the same condition would pull the instance out of
    # rotation anyway and undo that — an outage where a partial service was
    # intended. So: 200, `status: ok`, and the fault named in the body.
    client.app.state.config_database = _Database(False)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["config_database"] == CONFIG_DB_SCHEMA_MISSING
