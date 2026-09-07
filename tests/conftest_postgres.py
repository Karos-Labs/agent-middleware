"""A real PostgreSQL 16 for the Configuration API's tests.

Not a fake. The configuration schema's whole value is that the DATABASE
refuses things — a frozen version cannot be edited, a pointer cannot name a
draft, a client cannot be pinned to another agent's version, a tool with no
policy row is denied. A fake repository would let every one of those through
and the tests would pass while the guarantee was gone.

``pgserver`` ships a real server as a wheel, so this needs no Docker, no
service container and no network. The migrations that run are the ones in
``migrations/`` — the same files applied to Cloud SQL, which means the tests
fail if a migration is wrong rather than if a hand-written fixture schema is.

Skips, with a reason, when either half is missing:

* ``pgserver`` not installed — a CI image that has not picked up
  requirements-dev.txt yet.
* ``migrations/0001_config_plane.sql`` absent — S2's branch has not merged
  into this one yet. Every test here then reports as skipped rather than as
  passing, which is the honest signal.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.db.postgres import ConfigDatabase as _ConfigDatabase

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
REQUIRED = ("0001_config_plane.sql", "0002_reference_data.sql")


def _migrations_present() -> bool:
    return all((MIGRATIONS / name).exists() for name in REQUIRED)


def _skip_reason() -> str | None:
    if not _migrations_present():
        return (
            f"migrations/ does not carry {REQUIRED[0]} on this branch yet (S2 / "
            "SCRUM-217 has not merged into it), so there is no schema to test against"
        )
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return "pgserver is not installed (see requirements-dev.txt)"
    return None


requires_postgres = pytest.mark.skipif(
    _skip_reason() is not None, reason=_skip_reason() or ""
)


@pytest.fixture(scope="session")
def postgres_uri() -> Iterator[str]:
    """A running server, one per test session."""

    import pgserver

    data_dir = Path(os.environ.get("PYTEST_PG_DATA", "/tmp/agent-middleware-test-pg"))
    server = pgserver.get_server(data_dir)
    yield server.get_uri()


@pytest.fixture(scope="session")
def migrated_dsn(postgres_uri: str) -> Iterator[str]:
    """A database with the real migrations applied.

    Session-scoped and applied once: the migrations are the same for every test
    and applying them per test would spend most of the suite's time on DDL. The
    per-test fixture below truncates instead.
    """

    import pgserver

    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    database = "middleware_test"

    def run(dsn: str, *args: str) -> None:
        result = subprocess.run(
            [str(psql), dsn, "-q", "-v", "ON_ERROR_STOP=1", *args],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"psql failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}"
            )

    run(postgres_uri, "-c", f"drop database if exists {database}")
    run(postgres_uri, "-c", f"create database {database}")

    dsn = postgres_uri.replace("/postgres?", f"/{database}?")
    for name in sorted(p.name for p in MIGRATIONS.glob("0*.sql")):
        # `*_verify.sql` proves the guards work and rolls back; it is run by the
        # migration suite, not needed to set up a database to test against.
        if name.endswith("_verify.sql"):
            continue
        run(dsn, "-f", str(MIGRATIONS / name))

    yield dsn


@pytest.fixture
async def config_database(migrated_dsn: str) -> AsyncIterator[Any]:
    """A pool on a clean database.

    Truncated per test rather than re-migrated: `truncate ... cascade` on the
    mutable tables is milliseconds, where re-applying the DDL would make the
    suite mostly DDL. The reference data from 0002 (step kinds, agent classes,
    capability policy) survives, because every test needs it and nothing here
    mutates it.
    """

    from app.db.postgres import build_pool

    pool = await build_pool(migrated_dsn, min_size=1, max_size=4)
    database = _ConfigDatabase(pool)
    try:
        # audit_log and prompt_versions refuse UPDATE and DELETE by trigger, but
        # TRUNCATE is neither -- which is exactly why the guards are on those two
        # statements and not on "any write".
        await database.execute(
            """
            truncate table
                agent_version_steps, agent_versions, agents,
                agent_custom_agent_keys, client_agent_config, tool_config,
                prompt_versions, prompts, schedules, audit_log,
                model_aliases, models, tools
            restart identity cascade
            """
        )
        yield database
    finally:
        await pool.close()
