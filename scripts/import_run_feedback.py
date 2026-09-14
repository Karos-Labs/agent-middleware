"""Bring the Firestore ``run_feedback`` collection over to Postgres.

S11 / SCRUM-224. One-way, idempotent (the Firestore document id is kept as
``source_id``, which is UNIQUE), and reversible: nothing here writes to
Firestore, so dropping ``config.run_feedback`` leaves the collection exactly
as it is today.

Run it once after ``migrations/0006_run_feedback.sql`` has been applied and
``CONFIG_DB_DSN`` is set, and again any time a verdict is found to have landed
in Firestore after the cutover -- the second run writes nothing for what is
already here.

    python -m scripts.import_run_feedback --env prep --dry-run
    python -m scripts.import_run_feedback --env prep

Exits non-zero if any document was refused, so it is usable in a pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import Settings, get_settings
from app.db.firestore import FirestoreDB
from app.db.postgres import build_config_database
from app.services.feedback_import import FeedbackImporter, FeedbackImportReport
from app.services.runs import RunService

ENVIRONMENTS = ("prep", "prod")


def _print(report: FeedbackImportReport, *, dry_run: bool) -> None:
    print("\n=== RUN FEEDBACK IMPORT" + (" (dry run)" if dry_run else "") + " ===\n")
    for source_id in report.imported:
        print(f"  + {source_id}")
    for source_id in report.already_present:
        print(f"  = {source_id}")
    for source_id, reason in report.refused:
        print(f"  ! {source_id}  {reason}")
    counts = report.counts()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print("\nsummary: " + (summary or "nothing to do"))


async def _run(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    config_db = await build_config_database(settings)
    if config_db is None:
        sys.exit(
            "CONFIG_DB_DSN is not set, so there is no configuration plane to import "
            "into. See SCRUM-216."
        )

    firestore = FirestoreDB(settings)
    importer = FeedbackImporter(firestore, config_db, RunService(firestore))
    try:
        report = await importer.run(dry_run=args.dry_run)
    finally:
        firestore.close()
        await config_db.close()

    _print(report, dry_run=args.dry_run)
    return 1 if report.refused else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be imported. Writes nothing."
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
