"""Bring the Firestore ``run_feedback`` collection over to Postgres (S11).

One-way and idempotent, in the shape S5 set for the registry import: every
row written here carries the Firestore document id in ``source_id``, which is
UNIQUE, so running this twice writes nothing the second time and a document
that arrives in Firestore after the cutover can still be brought over later.

Reversible for the same reason S5 is: nothing here touches the Firestore
collection. Drop ``config.run_feedback`` and the documents are exactly what
they were.

What is resolved on the way over is what ``PostgresFeedbackStore.insert``
resolves on a fresh verdict -- the ``config.agents`` slug for the agent, and
the prompt version the run was produced with -- so an imported verdict is as
joinable as a new one. A document whose run no longer exists is imported with
what the document itself says and nothing resolved; the verdict is still
history worth keeping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.exceptions import ResourceNotFoundError
from app.db.firestore import FEEDBACK, FirestoreDB, snapshot_to_dict
from app.db.postgres import ConfigDatabase
from app.services.feedback import PostgresFeedbackStore
from app.services.runs import RunService

logger = logging.getLogger(__name__)


@dataclass
class FeedbackImportReport:
    imported: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "imported": len(self.imported),
            "already_present": len(self.already_present),
            "refused": len(self.refused),
        }


class FeedbackImporter:
    def __init__(self, firestore: FirestoreDB, config_db: ConfigDatabase, runs: RunService) -> None:
        self._firestore = firestore
        self._db = config_db
        self._runs = runs

    async def run(self, *, dry_run: bool = False) -> FeedbackImportReport:
        report = FeedbackImportReport()
        async for snapshot in self._firestore.collection(FEEDBACK).stream():
            document = snapshot_to_dict(snapshot)
            source_id = str(document["id"])
            try:
                outcome = await self._import_one(source_id, document, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the rest
                report.refused.append((source_id, f"{type(exc).__name__}: {exc}"))
                logger.warning("run_feedback %s refused: %s", source_id, exc)
                continue
            (report.imported if outcome == "imported" else report.already_present).append(
                source_id
            )
        return report

    async def _import_one(
        self, source_id: str, document: dict[str, Any], *, dry_run: bool
    ) -> str:
        present = await self._db.fetchval(
            "select 1 from run_feedback where source_id = $1", source_id
        )
        if present:
            return "already_present"

        run_id = str(document["run_id"])
        agent_id = str(document["agent_id"])
        try:
            run = await self._runs.get(run_id, agent_id=agent_id)
        except ResourceNotFoundError:
            run = {"id": run_id, "agent_id": agent_id}

        if dry_run:
            return "imported"

        async with self._db.transaction() as connection:
            agent_slug = await PostgresFeedbackStore._agent_slug_for(connection, agent_id)
            engine_prompt_id, engine_version, prompt_version_id = (
                await PostgresFeedbackStore._prompt_version_for(connection, run)
            )
            created_at = document.get("created_at")
            await connection.execute(
                """
                insert into run_feedback (
                    run_id, agent_id, agent_slug, client_slug,
                    rating, status, correction_notes, corrected_output, reviewer, tags,
                    prompt_version_id, engine_prompt_id, engine_prompt_version,
                    promoted_example_id, source_id, imported_at, created_at
                ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                          $14, $15, now(), coalesce($16::timestamptz, now()))
                """,
                run_id,
                agent_id,
                agent_slug,
                run.get("client_slug") if isinstance(run.get("client_slug"), str) else None,
                int(document["rating"]),
                str(document["status"]),
                document.get("correction_notes"),
                document.get("corrected_output"),
                document.get("reviewer"),
                [str(t) for t in (document.get("tags") or [])],
                prompt_version_id,
                engine_prompt_id,
                engine_version,
                document.get("promoted_example_id"),
                source_id,
                created_at if isinstance(created_at, datetime) else None,
            )
        return "imported"
