"""Feedback and evaluation store.

Every reviewer verdict on a run is kept so it can be mined later: the highest
rated (or reviewer-corrected) outputs are exactly the material a few-shot example
should be made of, and ``promote`` turns one into an example in place, recording
the link in both directions.

## Where a verdict lives (S11 / SCRUM-224)

Feedback on a RUN is a human judgement promoted to a few-shot example: low
volume, it needs a join to the prompt version it criticised, and the history
must be preserved. That is the configuration boundary, so it lives in Postgres
(``config.run_feedback``, migration 0006) when the configuration database is
wired -- with ``prompt_version_id`` resolved at write time to the newest
``prompt_versions`` row of the run's engine prompt created at or before the run.

Without a configuration database the verdicts stay in the Firestore root
collection they have always used. That is not a degraded mode; it is the normal
state of an environment where Cloud SQL does not exist yet (S1), and the same
arrangement every other Postgres-backed service in this repo makes. Which store
is in use is logged once at startup and is the only thing that differs: the
API's contract is identical over both, and ``tests/test_feedback.py`` runs the
same assertions against each.

Feedback on a DRAFT (the portal's ``xDraftFeedback`` and friends) is a different
thing and is not this module's concern.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Protocol

import asyncpg
from google.api_core.exceptions import FailedPrecondition
from google.cloud.firestore_v1.base_query import FieldFilter

from app.api.schemas.prompt import FewShotExampleCreate
from app.api.schemas.run import FeedbackCreate, FeedbackPromoteRequest
from app.core.enums import ExampleSource, FeedbackStatus
from app.core.exceptions import InvalidStateError, ResourceNotFoundError
from app.db.firestore import FEEDBACK, FirestoreDB, generate_id, snapshot_to_dict, utcnow
from app.db.postgres import ConfigDatabase
from app.services.prompts import PromptService
from app.services.runs import RunService

logger = logging.getLogger(__name__)

# Keys an engine artifact commonly uses for its main text body.
_OUTPUT_TEXT_KEYS = ("content", "text", "html", "body", "output", "markdown")


class FeedbackStore(Protocol):
    """The four things a verdict store has to do. Everything else is derivation."""

    async def insert(self, run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, feedback_id: str) -> dict[str, Any] | None: ...

    async def mark_promoted(self, feedback_id: str, example_id: str) -> None: ...

    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]: ...

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        min_rating: int | None,
        status: FeedbackStatus | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` rows, best rated first. The caller asks for one more
        than it needs to learn whether a next page exists."""
        ...


class FirestoreFeedbackStore:
    """The root collection ``run_feedback``. What this module was until S11."""

    def __init__(self, db: FirestoreDB) -> None:
        self._db = db

    async def insert(self, run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
        feedback_id = generate_id()
        await self._db.document(FEEDBACK, feedback_id).set(document)
        return {**document, "id": feedback_id}

    async def get(self, feedback_id: str) -> dict[str, Any] | None:
        snapshot = await self._db.document(FEEDBACK, feedback_id).get()
        return snapshot_to_dict(snapshot) if snapshot.exists else None

    async def mark_promoted(self, feedback_id: str, example_id: str) -> None:
        await self._db.document(FEEDBACK, feedback_id).update(
            {"promoted_example_id": example_id, "updated_at": utcnow()}
        )

    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        # A single equality filter needs no composite index, so the ordering is
        # applied in process.
        feedback = [
            snapshot_to_dict(snapshot)
            async for snapshot in self._db.collection(FEEDBACK)
            .where(filter=FieldFilter("run_id", "==", run_id))
            .stream()
        ]
        feedback.sort(key=lambda item: item.get("created_at") or utcnow())
        return feedback

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        min_rating: int | None,
        status: FeedbackStatus | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        query = self._db.collection(FEEDBACK).where(
            filter=FieldFilter("agent_id", "==", agent_id)
        )
        if status is not None:
            query = query.where(filter=FieldFilter("status", "==", status.value))
        if min_rating is not None:
            query = query.where(filter=FieldFilter("rating", ">=", min_rating))
        if offset:
            query = query.offset(offset)

        # Ordering needs a composite index (agent_id + rating), declared in
        # firestore.indexes.json. If it is missing, Firestore fails the whole
        # query with FailedPrecondition -- which is how a page that merely
        # LISTS feedback returned 500 while the index sat undeployed.
        #
        # Degrade instead: fetch unordered and sort in process. Wrong at scale,
        # which is why the index exists, but a listing that cannot be sorted
        # should still list.
        try:
            return [
                snapshot_to_dict(snapshot)
                async for snapshot in query.order_by("rating", direction="DESCENDING")
                .limit(limit)
                .stream()
            ]
        except FailedPrecondition:
            logger.warning(
                "run_feedback is missing its composite index; returning unordered results. "
                "Deploy firestore.indexes.json.",
                extra={"agent_id": agent_id},
            )
            rows = [snapshot_to_dict(snapshot) async for snapshot in query.stream()]
            rows.sort(key=lambda r: r.get("rating") or 0, reverse=True)
            return rows[:limit]


class PostgresFeedbackStore:
    """``config.run_feedback`` (migration 0006).

    The database enforces what the Firestore version could only promise: a
    rating stays in 1..5, a status stays in the vocabulary, a verdict is never
    edited or deleted, and promotion happens once. This class therefore does
    not re-check any of that; it lets the constraint speak.
    """

    def __init__(self, db: ConfigDatabase) -> None:
        self._db = db

    async def insert(self, run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
        async with self._db.transaction() as connection:
            agent_slug = await self._agent_slug_for(connection, document["agent_id"])
            engine_prompt_id, engine_version, prompt_version_id = await self._prompt_version_for(
                connection, run
            )
            row = await connection.fetchrow(
                """
                insert into run_feedback (
                    run_id, agent_id, agent_slug, client_slug,
                    rating, status, correction_notes, corrected_output, reviewer, tags,
                    prompt_version_id, engine_prompt_id, engine_prompt_version
                ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                returning *
                """,
                document["run_id"],
                document["agent_id"],
                agent_slug,
                _client_slug_of(run),
                document["rating"],
                document["status"],
                document["correction_notes"],
                document["corrected_output"],
                document["reviewer"],
                list(document["tags"]),
                prompt_version_id,
                engine_prompt_id,
                engine_version,
            )
        assert row is not None  # `returning *` on a successful insert
        return _row_to_feedback(row)

    async def get(self, feedback_id: str) -> dict[str, Any] | None:
        if not _looks_like_uuid(feedback_id):
            # The API accepts any string; the column is a uuid. A shape that
            # cannot be a row here is "not found", not a 500 from the driver.
            return None
        row = await self._db.fetchrow("select * from run_feedback where id = $1", feedback_id)
        return _row_to_feedback(row) if row is not None else None

    async def mark_promoted(self, feedback_id: str, example_id: str) -> None:
        # The guard refuses a second promotion; the service checked first and
        # reports it as InvalidStateError, so this raising would be a race.
        await self._db.execute(
            "update run_feedback set promoted_example_id = $2 where id = $1",
            feedback_id,
            example_id,
        )

    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            "select * from run_feedback where run_id = $1 order by created_at, id", run_id
        )
        return [_row_to_feedback(row) for row in rows]

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        min_rating: int | None,
        status: FeedbackStatus | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            select * from run_feedback
             where agent_id = $1
               and ($2::smallint is null or rating >= $2)
               and ($3::text is null or status = $3)
             order by rating desc, created_at desc, id
             limit $4 offset $5
            """,
            agent_id,
            min_rating,
            status.value if status is not None else None,
            limit,
            offset,
        )
        return [_row_to_feedback(row) for row in rows]

    # --- Resolution ------------------------------------------------------

    @staticmethod
    async def _agent_slug_for(connection: asyncpg.Connection, agent_id: str) -> str | None:
        """The ``config.agents`` row S5 imported for this Firestore agent, if any.

        Best effort by design: an agent that has not been imported yet still
        takes feedback, it just cannot be joined on until it has.
        """

        return await connection.fetchval(
            "select slug from agents where source_id = $1 order by imported_at desc limit 1",
            agent_id,
        )

    @staticmethod
    async def _prompt_version_for(
        connection: asyncpg.Connection, run: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        """The prompt version that was in the box when this run happened.

        A run records the engine prompt id and its pinned version ("x-draft",
        2). S7 keeps every content version of that prompt under the row whose
        ``engine_prompt_id`` / ``engine_version`` match, append-only, so the one
        that produced this run is the newest created at or before the run.

        Returns the raw pair too, so a run whose prompt has no versions here yet
        still records what it was judged against.
        """

        engine_prompt_id = run.get("prompt_id")
        raw_version = run.get("prompt_version")
        if not isinstance(engine_prompt_id, str) or raw_version is None:
            return None, None, None
        engine_version = str(raw_version)

        run_at = run.get("created_at")
        prompt_version_id = await connection.fetchval(
            """
            select pv.id
              from prompts p
              join prompt_versions pv on pv.prompt_id = p.id
             where p.engine_prompt_id = $1
               and p.engine_version = $2
               and ($3::timestamptz is null or pv.created_at <= $3)
             order by pv.version desc
             limit 1
            """,
            engine_prompt_id,
            engine_version,
            run_at if isinstance(run_at, datetime) else None,
        )
        return engine_prompt_id, engine_version, prompt_version_id


class FeedbackService:
    """Stores reviewer verdicts and turns the good ones into training examples."""

    def __init__(
        self,
        db: FirestoreDB,
        runs: RunService,
        prompts: PromptService,
        config_database: ConfigDatabase | None = None,
    ) -> None:
        self._runs = runs
        self._prompts = prompts
        self._store: FeedbackStore
        if config_database is not None:
            self._store = PostgresFeedbackStore(config_database)
            logger.info("run feedback is stored in Postgres (config.run_feedback)")
        else:
            self._store = FirestoreFeedbackStore(db)
            logger.info(
                "run feedback is stored in Firestore (%s); CONFIG_DB_DSN is not set", FEEDBACK
            )

    @property
    def store_kind(self) -> str:
        """``"postgres"`` or ``"firestore"``. For health output and tests."""

        return "postgres" if isinstance(self._store, PostgresFeedbackStore) else "firestore"

    # --- Writes ------------------------------------------------------------

    async def add(self, agent_id: str, run_id: str, payload: FeedbackCreate) -> dict[str, Any]:
        """Record feedback for a run.

        The run must already be registered (via ``POST /agents/{id}/runs`` or by
        dispatching through ``POST /agents/{id}/jobs``); that is what ties the
        verdict to the prompt and template version it is judging.
        """

        run = await self._runs.get(run_id, agent_id=agent_id)

        now = utcnow()
        document = {
            "run_id": run_id,
            "agent_id": agent_id,
            "rating": payload.rating,
            "status": payload.status.value,
            "correction_notes": payload.correction_notes,
            "corrected_output": payload.corrected_output,
            "reviewer": payload.reviewer,
            "tags": payload.tags,
            "promoted_example_id": None,
            "created_at": now,
            "updated_at": now,
        }
        stored = await self._store.insert(run, document)

        logger.info(
            "Stored feedback %s for run %s (agent=%s rating=%s status=%s)",
            stored["id"],
            run_id,
            agent_id,
            payload.rating,
            payload.status.value,
        )
        return stored

    async def promote(
        self, agent_id: str, feedback_id: str, payload: FeedbackPromoteRequest
    ) -> dict[str, Any]:
        """Turn a piece of feedback into an active few-shot example."""

        feedback = await self.get(agent_id, feedback_id)
        if feedback.get("promoted_example_id"):
            raise InvalidStateError(
                f"feedback '{feedback_id}' has already been promoted to example "
                f"'{feedback['promoted_example_id']}'"
            )

        run = await self._runs.get(feedback["run_id"], agent_id=agent_id)
        user_input = payload.user_input or derive_input_text(run)
        assistant_output = payload.assistant_output or derive_output_text(run, feedback)

        if not user_input or not assistant_output:
            raise InvalidStateError(
                "cannot derive an example from this feedback; supply 'user_input' and "
                "'assistant_output' explicitly"
            )

        example = await self._prompts.create_example(
            agent_id,
            FewShotExampleCreate(
                user_input=user_input,
                assistant_output=assistant_output,
                label=payload.label or f"from run {feedback['run_id']}",
                tags=payload.tags,
                position=payload.position,
                extra={
                    "feedback_id": feedback_id,
                    "rating": feedback.get("rating"),
                    "reviewer": feedback.get("reviewer"),
                },
            ),
            source=ExampleSource.FEEDBACK,
            source_run_id=feedback["run_id"],
        )

        await self._store.mark_promoted(feedback_id, example["id"])
        logger.info("Promoted feedback %s to example %s", feedback_id, example["id"])
        return example

    # --- Reads -------------------------------------------------------------

    async def get(self, agent_id: str, feedback_id: str) -> dict[str, Any]:
        feedback = await self._store.get(feedback_id)
        if feedback is None or feedback.get("agent_id") != agent_id:
            # Do not leak the existence of another agent's feedback.
            raise ResourceNotFoundError("feedback", feedback_id)
        return feedback

    async def list_for_run(self, agent_id: str, run_id: str) -> list[dict[str, Any]]:
        """All verdicts on one run, oldest first."""

        await self._runs.get(run_id, agent_id=agent_id)
        return await self._store.list_for_run(run_id)

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        min_rating: int | None = None,
        status: FeedbackStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Feedback for an agent, best rated first, plus whether more follow."""

        items = await self._store.list_for_agent(
            agent_id, min_rating=min_rating, status=status, limit=limit + 1, offset=offset
        )
        return items[:limit], len(items) > limit

    async def candidate_examples(
        self,
        agent_id: str,
        *,
        min_rating: int = 4,
        status: FeedbackStatus | None = FeedbackStatus.APPROVED,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Feedback distilled into example candidates the portal can review.

        Defaults to approved, well-rated runs: the ones worth teaching the agent
        with. Each candidate carries the run's input and the best available
        output (the reviewer's correction when there is one).
        """

        feedback_items, has_more = await self.list_for_agent(
            agent_id, min_rating=min_rating, status=status, limit=limit, offset=offset
        )

        candidates: list[dict[str, Any]] = []
        for feedback in feedback_items:
            try:
                run = await self._runs.get(feedback["run_id"], agent_id=agent_id)
            except ResourceNotFoundError:
                run = {}

            candidates.append(
                {
                    "feedback_id": feedback["id"],
                    "run_id": feedback["run_id"],
                    "rating": feedback["rating"],
                    "status": feedback["status"],
                    "user_input": derive_input_text(run),
                    "assistant_output": derive_output_text(run, feedback),
                    "correction_notes": feedback.get("correction_notes"),
                    "reviewer": feedback.get("reviewer"),
                    "already_promoted": bool(feedback.get("promoted_example_id")),
                    "created_at": feedback["created_at"],
                }
            )
        return candidates, has_more


def _row_to_feedback(row: asyncpg.Record) -> dict[str, Any]:
    """A ``run_feedback`` row in the shape ``FeedbackRead`` has always had.

    The Postgres row carries more (the prompt-version join, the agent slug, the
    import provenance). Those ride along under their own names; nothing the
    Firestore document had is renamed or dropped, which is what keeps the two
    stores interchangeable behind one API.
    """

    feedback = dict(row)
    feedback["id"] = str(feedback["id"])
    if feedback.get("prompt_version_id") is not None:
        feedback["prompt_version_id"] = str(feedback["prompt_version_id"])
    feedback["tags"] = list(feedback.get("tags") or [])
    return feedback


def _client_slug_of(run: dict[str, Any]) -> str | None:
    value = run.get("client_slug")
    return value if isinstance(value, str) and value else None


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return len(value) == 36 and [len(p) for p in parts] == [8, 4, 4, 4, 12]


def derive_input_text(run: dict[str, Any]) -> str | None:
    """Best-effort textual rendering of what the run was asked to do."""

    payload = run.get("input_payload") or {}
    job_input = payload.get("input", payload) if isinstance(payload, dict) else payload
    return _as_text(job_input)


def derive_output_text(run: dict[str, Any], feedback: dict[str, Any]) -> str | None:
    """The reviewer's correction if present, else what the run produced."""

    corrected = (feedback.get("corrected_output") or "").strip()
    if corrected:
        return corrected

    output = run.get("output")
    if isinstance(output, dict):
        for key in _OUTPUT_TEXT_KEYS:
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return _as_text(output)


def _as_text(value: Any) -> str | None:
    """Render a stored JSON value as text, or ``None`` when there is nothing to show."""

    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
