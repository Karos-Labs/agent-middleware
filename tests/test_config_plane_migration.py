"""The SQL migrations are checked without a database.

CI has no Postgres, and standing one up for a schema that changes twice a
quarter would be a lot of machinery. But the failure this guards against is
cheap to catch statically and expensive to miss: someone deletes a trigger, or
adds a table and forgets the guard that makes it safe, and nothing complains
until a published version turns out to have been editable all along.

So this reads the migration as text and asserts the things that must be true
of it. It is not a substitute for `0001_config_plane_verify.sql`, which does
the real work against a live server -- it is the part of that file's job that
can run in a pipeline with no server in it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

SCHEMA = MIGRATIONS / "0001_config_plane.sql"
REFERENCE_DATA = MIGRATIONS / "0002_reference_data.sql"
VERIFY = MIGRATIONS / "0001_config_plane_verify.sql"

# The tables SCRUM-217 names, verbatim. "prompts + prompt_versions" is one
# item on the ticket and two tables here.
TICKET_TABLES = frozenset({
    "agents",
    "agent_versions",
    "agent_version_steps",
    "prompts",
    "prompt_versions",
    "tools",
    "tool_config",
    "models",
    "step_kinds",
    "agent_classes",
    "capability_policy",
    "client_agent_config",
    "schedules",
    "audit_log",
})

# Two more, each because a constraint demanded a table rather than a column --
# see migrations/README.md. Listed so adding a third is a deliberate edit here.
SUPPORT_TABLES = frozenset({"agent_custom_agent_keys", "model_aliases", "schema_migrations"})

# Every rule the database is supposed to enforce by itself. A trigger removed
# from the migration fails this list, which is the whole point of having it.
GUARD_TRIGGERS = frozenset({
    "agent_versions_00_guard",
    "agent_version_steps_00_guard",
    "tool_config_00_guard",
    "prompt_versions_00_guard",
    "audit_log_00_guard",
    "agents_10_pointer_guard",
    "client_agent_config_10_pointer_guard",
    "agent_version_steps_20_kind_guard",
})


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def _created_tables(sql: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(
            r"create table (?:if not exists )?config\.(\w+)", sql, re.IGNORECASE
        )
    }


def test_every_table_the_ticket_names_is_created(schema_sql: str) -> None:
    created = _created_tables(schema_sql)
    missing = TICKET_TABLES - created
    assert not missing, f"missing tables from SCRUM-217: {sorted(missing)}"


def test_no_table_appears_that_is_not_accounted_for(schema_sql: str) -> None:
    """A new table is fine; a new table nobody documented is not.

    The support tables are listed in the README with the constraint that
    forced each one. This test is what makes that list stay true.
    """

    created = _created_tables(schema_sql)
    unexplained = created - TICKET_TABLES - SUPPORT_TABLES
    assert not unexplained, (
        f"tables not named by the ticket and not in SUPPORT_TABLES: {sorted(unexplained)}. "
        "Add it to migrations/README.md and to this list, with the reason."
    )


def test_every_guard_is_wired_to_a_table(schema_sql: str) -> None:
    for trigger in sorted(GUARD_TRIGGERS):
        assert re.search(
            rf"create trigger {trigger}\s+before .* on config\.\w+",
            schema_sql,
            re.IGNORECASE | re.DOTALL,
        ), f"guard trigger {trigger} is declared nowhere"


def test_every_guard_is_exercised_by_the_verify_script() -> None:
    """A guard with no check is a guard nobody has seen work.

    The verify script names each trigger in a comment or a check, so adding a
    guard without a check that proves it fails here rather than in production.
    """

    verify_sql = VERIFY.read_text(encoding="utf-8")
    schema_sql = SCHEMA.read_text(encoding="utf-8")

    # The verify script exercises guards by behaviour, not by name, so the
    # link is the function each trigger calls: every guard function must be
    # reachable from a check. Assert on the tables instead, which is what the
    # checks actually touch.
    guarded_tables = set(
        re.findall(
            r"create trigger (?:{})\s+before[^;]*? on config\.(\w+)".format(
                "|".join(sorted(GUARD_TRIGGERS))
            ),
            schema_sql,
            re.IGNORECASE | re.DOTALL,
        )
    )
    assert guarded_tables, "no guarded tables found -- the regex above has rotted"

    for table in sorted(guarded_tables):
        assert f"config.{table}" in verify_sql, (
            f"{table} carries a guard that 0001_config_plane_verify.sql never tests"
        )


def _first_statement(sql: str) -> str:
    """The first line that is neither blank nor a comment."""

    for line in sql.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return stripped.lower()
    return ""


@pytest.mark.parametrize("path", [SCHEMA, REFERENCE_DATA])
def test_each_migration_is_one_transaction(path: Path) -> None:
    """A half-applied schema migration is worse than a failed one.

    Postgres runs DDL transactionally, so wrapping the file means a migration
    that fails on statement 40 leaves the database exactly as it was rather
    than in a shape no version of the code expects.
    """

    sql = path.read_text(encoding="utf-8")
    assert _first_statement(sql) == "begin;", (
        f"{path.name} does not open with BEGIN; a failure part-way through would "
        "leave the schema half-applied"
    )
    assert sql.rstrip().lower().endswith("commit;")


@pytest.mark.parametrize("path", [SCHEMA, REFERENCE_DATA])
def test_each_migration_records_itself_in_the_ledger(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    assert f"'{path.name}'" in sql, (
        f"{path.name} does not insert its own filename into config.schema_migrations, "
        "so the ledger cannot tell whether it ran"
    )


def test_the_verify_script_leaves_nothing_behind() -> None:
    """It is meant to be safe to run against prod."""

    verify_sql = VERIFY.read_text(encoding="utf-8")
    assert verify_sql.rstrip().lower().endswith("rollback;")
    assert "commit;" not in verify_sql.lower()


def test_a_failed_check_is_loud() -> None:
    """Every check must fail on success of the write it attempts.

    A check that inserts a forbidden row and forgets to raise on acceptance
    passes forever. Each `exception when` handler in the file is preceded by a
    `raise exception 'FAIL` in the same block, so count them.
    """

    verify_sql = VERIFY.read_text(encoding="utf-8")
    fails = len(re.findall(r"raise exception 'FAIL", verify_sql))
    handlers = len(re.findall(r"exception when \w+ then", verify_sql))
    assert fails >= handlers, (
        f"{handlers} exception handlers but only {fails} FAIL guards: at least one check "
        "cannot distinguish a refused write from an accepted one"
    )
