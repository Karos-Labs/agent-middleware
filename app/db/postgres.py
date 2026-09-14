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

Authentication is read off the DSN. Cloud Run's Cloud SQL integration mounts a
socket and does not log anybody in, so a DSN naming an IAM database user with
no password gets an access token presented as its password, refreshed per
connection (:class:`AccessTokenPassword`). A DSN with a password is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

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


#: The scope a token has to carry to be accepted as a Cloud SQL login.
#:
#: `cloud-platform` also works, but a token minted for it is a token that can
#: do everything this service account can do, handed to a database. This one
#: logs in and nothing else.
IAM_LOGIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"


class _Credentials(Protocol):
    """The slice of ``google.auth`` credentials this module actually uses."""

    token: str | None
    valid: bool

    def refresh(self, request: Any) -> None: ...


def iam_database_user(dsn: str) -> str | None:
    """The IAM database user in ``dsn``, or ``None`` for password auth.

    Cloud SQL names an IAM service account user after the account with
    ``.gserviceaccount.com`` cut off, so
    ``agent-middleware-sa@karoscmo-prep.iam.gserviceaccount.com`` logs in as
    ``agent-middleware-sa@karoscmo-prep.iam``. That suffix is what identifies
    the mode, and a DSN that carries a password is answering the question
    already -- built-in authentication, whatever the user is called.

    A DSN naming an IAM user and carrying no password has exactly one reading.
    There is no third possibility to configure a flag for: a built-in user with
    an empty password cannot connect either, so a variable saying which mode to
    use could only ever disagree with the DSN and lose.
    """

    parsed = urlsplit(dsn)
    if parsed.password:
        return None
    username = unquote(parsed.username or "")
    return username if username.endswith(".iam") else None


class AccessTokenPassword:
    """An OAuth access token, handed to asyncpg as the password.

    Cloud Run's built-in Cloud SQL socket does the *transport* -- it dials the
    instance and puts a unix socket in ``/cloudsql`` -- and stops there. It does
    not authenticate anybody. Logging in as an IAM database user means
    presenting a short-lived access token where a password would go, and that
    is the whole of what this class is for.

    It is a callable rather than a string because a token expires in an hour and
    a pool outlives that. asyncpg calls it for every new connection, so a
    connection opened at the fifty-ninth minute gets a fresh token and the
    process never has to know it happened. The cached credentials refresh
    themselves only once ``valid`` goes false, so the common case is a dict
    lookup rather than a metadata-server round trip.

    The blocking parts of ``google.auth`` run in a thread. A refresh talks to
    the metadata server, and doing that on the event loop stalls every request
    in flight behind a network call nobody can see.
    """

    def __init__(
        self,
        credentials: _Credentials | None = None,
        request: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._request = request
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._credentials is None:
                self._credentials = await asyncio.to_thread(self._default)
            if not self._credentials.valid:
                await asyncio.to_thread(
                    self._credentials.refresh, self._request or self._transport()
                )
            token = self._credentials.token

        if not token:
            # asyncpg would send an empty password and the server would answer
            # "password authentication failed", which reads as a wrong password
            # rather than as a token that never arrived.
            raise RuntimeError(
                "no access token for the configuration database: the credentials "
                "refreshed without producing one"
            )
        return token

    @staticmethod
    def _default() -> _Credentials:
        import google.auth

        credentials, _ = google.auth.default(scopes=[IAM_LOGIN_SCOPE])
        return credentials  # type: ignore[return-value]

    @staticmethod
    def _transport() -> Any:
        from google.auth.transport.requests import Request

        return Request()


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
    password: AccessTokenPassword | str | None = None,
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
        # None is what asyncpg defaults to, and it means "whatever the DSN and
        # the environment say" -- so passing it through changes nothing for a
        # password DSN.
        password=password,
        init=_prepare,
    )
    if pool is None:  # pragma: no cover - asyncpg only returns None on failure
        raise RuntimeError(f"could not build a connection pool for {dsn!r}")
    return pool


async def build_config_database(settings: Settings) -> ConfigDatabase | None:
    """A pool, or ``None`` when there is no usable configuration database.

    ``None`` is not a failure. Cloud SQL does not exist in every environment
    yet (S1 / SCRUM-216), and a control plane that refuses to start without it
    would make the Postgres migration a flag day for routes that have nothing
    to do with it.

    That holds for a database that is configured and unreachable too, which is
    why the connection is inside a ``try``. This runs in the lifespan, so an
    exception here is a container that never becomes ready -- and Cloud Run
    cold-starts constantly. Letting the pool raise would turn a Cloud SQL blip
    into a total outage of a control plane whose other thirty routes only ever
    talk to Firestore, and would do it minutes after the blip rather than
    during it, on the next cold start, when nobody is looking at Cloud SQL any
    more.

    It is logged at ERROR rather than swallowed, and it is the same shape as
    the schema-missing branch below, which already degrades this way. Crashing
    on one and degrading on the other would be the worst of both: an outage
    where a 503 was expected.
    """

    dsn = settings.config_db_dsn
    if not dsn:
        logger.info(
            "CONFIG_DB_DSN is not set; the Configuration API will report itself "
            "unavailable and every other route is unaffected"
        )
        return None

    iam_user = iam_database_user(dsn)
    password: AccessTokenPassword | None = None
    if iam_user:
        password = AccessTokenPassword()
        logger.info(
            "authenticating to the configuration database as the IAM database "
            "user %s; every new connection presents a freshly minted access "
            "token as its password",
            iam_user,
        )

    try:
        pool = await build_pool(
            dsn,
            password=password,
            min_size=settings.config_db_pool_min_size,
            max_size=settings.config_db_pool_max_size,
            # Cloud Run scales to zero and a pooled connection outlives that; a
            # connection that has been idle longer than Cloud SQL's own timeout
            # is a first-request 500 that looks like a code fault.
            idle_lifetime=settings.config_db_idle_lifetime_seconds,
            command_timeout=settings.config_db_command_timeout_seconds,
        )
    except Exception as error:  # noqa: BLE001 - breadth is the point
        # Everything that can go wrong here is a reason to serve without the
        # configuration database rather than not to serve: a wrong password, an
        # instance that was never attached to the revision, a socket that is not
        # in /cloudsql, an access token that would not mint, a network that is
        # down. Naming a subset would leave the unnamed ones taking the service
        # down, and the log line says which one it was either way.
        logger.error(
            "could not connect to the configuration database (%s: %s) -- the "
            "Configuration API will report itself unavailable and every other "
            "route is unaffected. Check that the revision has the Cloud SQL "
            "instance attached, that CONFIG_DB_DSN points at it, and that the "
            "runtime service account holds roles/cloudsql.client and "
            "roles/cloudsql.instanceUser",
            type(error).__name__,
            error,
        )
        return None

    database = ConfigDatabase(pool)

    if not await database.schema_is_applied():
        logger.error(
            "connected to the configuration database but the '%s' schema is not "
            "there -- apply migrations/0001_config_plane.sql before serving",
            SCHEMA,
        )

    return database
