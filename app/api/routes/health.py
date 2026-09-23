"""Liveness and readiness endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])

logger = logging.getLogger(__name__)

#: The configuration database's four distinguishable states.
#:
#: They are not degrees of the same thing and they do not share a fix:
#: ``not_configured`` is an environment that has no Cloud SQL yet and is
#: behaving correctly; ``unavailable`` is a pool that would not build, which is
#: an instance, a socket or a grant; ``schema_missing`` is a database that
#: exists and is EMPTY, which is a migration nobody ran.
CONFIG_DB_NOT_CONFIGURED = "not_configured"
CONFIG_DB_UNAVAILABLE = "unavailable"
CONFIG_DB_SCHEMA_MISSING = "schema_missing"
CONFIG_DB_OK = "ok"


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """Simple liveness probe: the process is up and serving requests."""

    return {"status": "ok"}


async def config_database_state(request: Request) -> str:
    """What the configuration database is doing, as one word.

    ## Why this is here at all

    Production ran for weeks with **zero tables** in `karos-prod` — not one
    migration had ever been applied — while this endpoint answered
    `{"status":"ok","firestore_reachable":true}` and the deploy's "Verify the
    new revision is serving" step passed every time. Nothing was lying: the
    service really was serving, and thirty of its routes really were fine.
    Readiness simply never asked about the other store, so "is this healthy"
    had no way to return the one answer that mattered.

    ## Why it re-checks instead of reporting a cached verdict

    `build_config_database` already logs this at startup, and caching that
    verdict would be cheaper. But the fix for `schema_missing` is to run the
    migrations, and a cached verdict would keep reporting the fault until
    somebody also redeployed — which is exactly the kind of stale signal that
    teaches people to ignore the signal. One `select exists(...)` against
    `information_schema` per probe is the price of an answer that is true when
    it is read.
    """

    database = getattr(request.app.state, "config_database", None)
    if database is None:
        # Both "no DSN" and "the pool would not build" arrive here as None.
        # `build_config_database` logs which, at ERROR for the second; the
        # settings say which of the two this environment should be.
        settings = getattr(request.app.state, "settings", None)
        configured = bool(getattr(settings, "config_db_dsn", None))
        return CONFIG_DB_UNAVAILABLE if configured else CONFIG_DB_NOT_CONFIGURED
    try:
        return CONFIG_DB_OK if await database.schema_is_applied() else CONFIG_DB_SCHEMA_MISSING
    except Exception as error:  # noqa: BLE001 - a probe never raises
        # A readiness probe that 500s is a probe nobody can build on. The pool
        # existed at startup and does not answer now, which is `unavailable`.
        logger.warning(
            "configuration database schema check failed (%s: %s)",
            type(error).__name__,
            error,
        )
        return CONFIG_DB_UNAVAILABLE


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe: can this instance actually reach its store?

    Firestore is the only hard dependency of a control plane read. Pub/Sub is
    deliberately not probed: publishing happens on dispatch, and a broker blip
    should surface as a 502 on that one call rather than pulling the whole
    instance out of rotation.

    THE CONFIGURATION DATABASE IS REPORTED BUT NEVER FAILS THE PROBE, and the
    asymmetry is deliberate. `build_config_database`'s own comment explains the
    design: a control plane whose other thirty routes only talk to Firestore
    must keep serving them when Cloud SQL is missing, so it degrades rather
    than refuses to start. Failing readiness on the same condition would undo
    that by pulling the instance out of rotation anyway — an outage where a
    partial service was intended. So it is a FIELD, for a deploy check and a
    human to read, not a verdict about rotation.
    """

    database = getattr(request.app.state, "db", None)
    firestore_ready = await database.ping() if database is not None else False

    payload = {
        "status": "ok" if firestore_ready else "degraded",
        "firestore_reachable": firestore_ready,
        "config_database": await config_database_state(request),
    }
    return JSONResponse(
        content=payload,
        status_code=status.HTTP_200_OK if firestore_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
