"""PostgreSQL access layer for the configuration plane.

Firestore holds everything the control plane serves today. Postgres holds the
one thing it cannot: an agent *version*. Forty steps of twenty kilobytes of
prompt is 800KB against Firestore's 1MB document ceiling, and splitting a
version into a subcollection loses the atomic write that is the only thing
making a version a version (S2 / SCRUM-217).

This module is the only place that knows about asyncpg. The service above it
works with a connection handed out here, and every statement it runs is
written against the ``config`` schema the migrations created.

Two things are set per connection rather than per query:

* ``search_path = config, public`` -- so a query says ``agent_versions``
  rather than ``config.agent_versions`` fifty times, and a table that moves
  schema is one line here.
* ``jsonb`` codecs -- asyncpg hands back ``str`` for jsonb by default, which
  means every read site remembers to ``json.loads``. One of them eventually
  does not.

Absent by design when unconfigured. ``build_config_database`` returns ``None``
without a DSN, the Configuration API's routes 503 with a message saying so, and
every existing route keeps working -- the migration is additive, and an
environment where Cloud SQL does not exist yet is a normal state rather than a
broken one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.config import Settings

logger = logging.getLogger(__name__)

SCHEMA = "config"


#: Set as a CONNECTION PARAMETER rather than with `SET search_path = ...`.
#:
#: asyncpg issues `RESET ALL` when a connection goes back to the pool, which
#: discards every session GUC -- including a search_path set in `init`. The
#: first query on a recycled connection then fails with `relation
#: "agent_versions" does not exist`, intermittently, depending on whether the
#: pool handed out a fresh connection or a reused one. A startup parameter is
#: what `RESET ALL` resets *to*, so it survives.
SERVER_SETTINGS = {"search_path": f"{SCHEMA}, public"}


async def _prepare(connection: asyncpg.Connection) -> None:
    """Per-connection setup that survives the pool's reset.

    Type codecs only. The search_path is a connection parameter (see
    :data:`SERVER_SETTINGS`) because anything set with `SET` here is discarded
    the moment the connection is released.
    """

    for typename in ("json", "jsonb"):
        await connection.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


class ConfigDatabase:
    """A pool over the configuration schema.

    Thin on purpose. There is no ORM and no query builder: the queries here are
    against a schema whose constraints are the point, and an abstraction that
    lets someone write them without seeing the constraint is the abstraction
    that produces a runtime error instead of a compile-time one.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """One transaction, committed on a clean exit and rolled back otherwise.

        This is what "in a SINGLE transaction: freeze the version, mark the
        previous one superseded, move the pointer, and write an audit record"
        (SCRUM-218) is built on. A publish that fails half-way must leave a
        draft, not a frozen version nobody pointed at.
        """

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                yield connection

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.connection() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self.connection() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.connection() as connection:
            return await connection.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.connection() as connection:
            return await connection.execute(query, *args)

    async def close(self) -> None:
        await self._pool.close()

    async def schema_is_applied(self) -> bool:
        """Whether the migrations have run against this database.

        Checked at startup rather than on first request, because "the DSN is
        right and the schema is missing" and "the DSN is wrong" produce very
        different fixes and the same 500 if nobody looks.
        """

        return bool(
            await self.fetchval(
                "select exists ("
                "  select 1 from information_schema.tables"
                "  where table_schema = $1 and table_name = 'agent_versions')",
                SCHEMA,
            )
        )


async def build_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 5,
    idle_lifetime: float = 300.0,
    command_timeout: float = 30.0,
) -> asyncpg.Pool:
    """A pool with this module's per-connection setup applied.

    Separate from :func:`build_config_database` so a test can have a pool with
    the same jsonb codecs and the same ``search_path`` as production without
    going through ``Settings`` -- a test that configures its connection
    differently from the service is a test of something else.
    """

    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        max_inactive_connection_lifetime=idle_lifetime,
        command_timeout=command_timeout,
        server_settings=SERVER_SETTINGS,
        init=_prepare,
    )
    if pool is None:  # pragma: no cover - asyncpg only returns None on failure
        raise RuntimeError(f"could not build a connection pool for {dsn!r}")
    return pool


async def build_config_database(settings: Settings) -> ConfigDatabase | None:
    """A pool, or ``None`` when no DSN is configured.

    ``None`` is not a failure. Cloud SQL does not exist in every environment
    yet (S1 / SCRUM-216), and a control plane that refuses to start without it
    would make the Postgres migration a flag day for routes that have nothing
    to do with it.
    """

    dsn = settings.config_db_dsn
    if not dsn:
        logger.info(
            "CONFIG_DB_DSN is not set; the Configuration API will report itself "
            "unavailable and every other route is unaffected"
        )
        return None

    pool = await build_pool(
        dsn,
        min_size=settings.config_db_pool_min_size,
        max_size=settings.config_db_pool_max_size,
        # Cloud Run scales to zero and a pooled connection outlives that; a
        # connection that has been idle longer than Cloud SQL's own timeout is
        # a first-request 500 that looks like a code fault.
        idle_lifetime=settings.config_db_idle_lifetime_seconds,
        command_timeout=settings.config_db_command_timeout_seconds,
    )
    database = ConfigDatabase(pool)

    if not await database.schema_is_applied():
        logger.error(
            "connected to the configuration database but the '%s' schema is not "
            "there -- apply migrations/0001_config_plane.sql before serving",
            SCHEMA,
        )

    return database
