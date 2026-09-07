"""Import agent configuration from the registries it is scattered across.

S5 / SCRUM-220. One-way, idempotent, and reversible: it writes only into the
``config`` schema, so dropping that schema leaves the Firestore documents as
the source of truth exactly as they are today. That is the ticket's own test
for whether the Postgres decision can be undone, and it is a property of this
script rather than a promise about it.

Read :mod:`app.services.registry_import` for where the five registries
actually are and why the thirteen hand-written engine workflows import as an
agent row and its prompts but NOT as a version. The short version: a step here
must satisfy its kind, and a compiled workflow's stage list cannot -- so the
alternative to refusing is inventing a prompt or a script for ~260 stages and
letting S6 freeze the invention as fact.

    python -m scripts.import_registries --env prep --dry-run
    python -m scripts.import_registries --env prep

Exits non-zero if any agent was refused, so it is usable in a pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import Settings, get_settings
from app.db.firestore import FirestoreDB
from app.db.postgres import build_config_database
from app.services.configuration import ConfigurationService
from app.services.prompt_store import UnifiedPromptStore
from app.services.registry_import import ImportReport, RegistryImporter

ENVIRONMENTS = ("prep", "prod")


def _print(report: ImportReport) -> None:
    print("\n=== REGISTRY IMPORT ===\n")
    for outcome in report.outcomes:
        mark = {
            "imported": "+",
            "unchanged": "=",
            "row_only": "~",
            "refused": "!",
        }.get(outcome.result, "?")
        version = f" v{outcome.version}" if outcome.version else ""
        print(
            f"  {mark} {outcome.slug:<28} {outcome.result:<10}{version}"
            f"  [{outcome.stage_source}]"
            f"  prompts={outcome.prompts_imported} keys={outcome.custom_agent_keys}"
        )
        for reason in outcome.reasons:
            print(f"        - {reason}")

    print("\n--- WHAT EACH RESULT MEANS ---")
    print("  + imported   a version was created, validated and published")
    print("  = unchanged  the published version already matches; nothing written")
    print("  ~ row_only   the agent row, its portal keys and its prompts were")
    print("               imported. No version, because its stage list is a")
    print("               compiled workflow -- see stage_source=engine_code.")
    print("  ! refused    nothing was published. Every reason is listed above.")

    counts = report.counts()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print("\nsummary: " + (summary or "nothing to do"))
    if report.refused:
        print(
            f"\n{len(report.refused)} agent(s) refused. Nothing about them was "
            "published, and the drafts are left in place so the problems can be "
            "read off the version."
        )


async def _run(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    config_db = await build_config_database(settings)
    if config_db is None:
        sys.exit(
            "CONFIG_DB_DSN is not set, so there is no configuration plane to import "
            "into. See SCRUM-216."
        )

    firestore = FirestoreDB(settings)
    prompts = UnifiedPromptStore(config_db, firestore)
    importer = RegistryImporter(
        config_db, firestore, ConfigurationService(config_db), prompts
    )
    try:
        report = await importer.run(actor=args.actor, dry_run=args.dry_run)
    finally:
        firestore.close()

    _print(report)
    return 1 if report.refused else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported. Writes nothing.",
    )
    parser.add_argument(
        "--actor",
        default="scripts/import_registries",
        help="Recorded on every row and audit entry this run creates.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
