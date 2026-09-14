"""Import karosCMO's two scheduling collections into ``config.schedules``.

S10 / SCRUM-223. One-way, idempotent (``source_system`` + ``source_id``), and
reversible: nothing here writes to Firestore, so dropping the rows leaves both
collections exactly as they are. Read :mod:`app.services.schedule_import` for
what is refused and why -- in short, this script does not guess who pays, what
zone a schedule meant, or where a one-off run belongs.

    python -m scripts.import_schedules --env prep --dry-run
    python -m scripts.import_schedules --env prep --assume-time-zone Asia/Jerusalem

Exits non-zero if any document was refused, so it is usable in a pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import Settings, get_settings
from app.db.firestore import FirestoreDB
from app.db.postgres import build_config_database
from app.services.schedule_import import ScheduleImporter, ScheduleImportReport

ENVIRONMENTS = ("prep", "prod")


def _print(report: ScheduleImportReport, *, dry_run: bool) -> None:
    print("\n=== SCHEDULE IMPORT" + (" (dry run)" if dry_run else "") + " ===\n")
    for outcome in report.outcomes:
        mark = {"imported": "+", "already_present": "=", "refused": "!"}.get(outcome.result, "?")
        print(f"  {mark} {outcome.source_system:<22} {outcome.source_id:<28} {outcome.result}")
        for reason in outcome.reasons:
            print(f"        - {reason}")
    counts = report.counts()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print("\nsummary: " + (summary or "nothing to do"))
    if report.refused:
        print(
            f"\n{len(report.refused)} document(s) refused. Each reason above is a question "
            "for a person -- who pays, which zone, where a one-off run belongs -- and "
            "this script does not answer those."
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
    importer = ScheduleImporter(firestore, config_db, assume_time_zone=args.assume_time_zone)
    try:
        report = await importer.run(actor=args.actor, dry_run=args.dry_run)
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
    parser.add_argument("--dry-run", action="store_true", help="Report only. Writes nothing.")
    parser.add_argument(
        "--assume-time-zone",
        default=None,
        help=(
            "IANA zone for rows that carry none. Without it those rows are refused, "
            "because a schedule's zone is its intent and this script does not have the client's."
        ),
    )
    parser.add_argument("--actor", default="scripts/import_schedules")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
