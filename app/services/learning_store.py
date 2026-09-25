"""The learning tables (migration 0007), as the projector and collector read them.

Every method here is one query against the ``config`` schema and returns
plain dicts in the shapes C7 (``docs/contracts/C7-run-context.md``) projects,
so the projector is a serialiser and the collector a mapper -- neither holds
SQL. Rows are returned camelCased under the names the engine reads: a column
and an engine field with the same meaning have the same name (C7 §4.5), and
the one place the two spellings meet is this module.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.db.postgres import ConfigDatabase
from app.services.voice_lessons import derive_voice_notes, likes_from_posts

logger = logging.getLogger(__name__)

#: The engine's platform keys (C7 §2). The CHECK constraints in 0007 hold the
#: same list; this one is for the product-id mapping below.
PLATFORMS: tuple[str, ...] = ("x", "linkedin", "reddit", "instagram", "tiktok")

FUNNEL_STAGES: tuple[str, ...] = ("attention", "expertise", "decide")
FEEDBACK_ACTIONS: tuple[str, ...] = (
    "posted",
    "posted_with_edits",
    "skipped",
    "change_requested",
    "note",
)
SUBJECT_STATUSES: tuple[str, ...] = (
    "drafted",
    "approved",
    "posted",
    "skipped",
    "change_requested",
)

#: A review action moves the subject row it was about (B1 ↔ B2).
STATUS_FOR_ACTION: dict[str, str] = {
    "posted": "posted",
    "posted_with_edits": "posted",
    "skipped": "skipped",
    "change_requested": "change_requested",
}


#: Products whose id does not spell out its platform.
#:
#: D08 split TikTok into three agents -- clipping, editing, content design --
#: and every one of them posts to the SAME TikTok account. The stores are keyed
#: on the PLATFORM, not the product, because a client has one account and one
#: subject history on it, not one per product we happen to sell. So all three
#: map to ``tiktok``, and adding a fourth TikTok product is one line here and
#: nothing else: no migration, no enum, no projection change.
#:
#: ``branded-shorts-agent`` is the pre-rename id of the editing agent and maps
#: to the same key, which also fixes a real gap: until this table existed it
#: mapped to ``None``, so every branded-shorts run was dispatched with no
#: learning context and collected nothing.
PRODUCT_PLATFORM_OVERRIDES: dict[str, str] = {
    "tiktok-clipping-agent": "tiktok",
    "tiktok-editing-agent": "tiktok",
    "tiktok-content-design-agent": "tiktok",
    "branded-shorts-agent": "tiktok",
}


def platform_for_product(product_id: str | None) -> str | None:
    """``x-agent`` → ``x``; anything that is not a platform agent → ``None``.

    The agent's slug IS the engine's product id (see ``dispatch._build_payload``).
    Most platform agents are named ``<platform>-agent`` and resolve by their own
    spelling; the rest are named in ``PRODUCT_PLATFORM_OVERRIDES`` above, which
    is consulted FIRST so a product can never be routed by an accident of its
    name.

    A product with no platform -- the SEO audit, the landing builder, the
    orchestrator -- has no learning context to project and nothing to collect,
    and the caller skips it rather than projecting under a made-up key.

    Getting this wrong is the worst failure mode in this service, because the
    symptom is a run that works: it drafts, it delivers, and it silently learns
    nothing.
    """

    if not product_id:
        return None
    override = PRODUCT_PLATFORM_OVERRIDES.get(product_id)
    if override is not None:
        return override
    head, sep, tail = product_id.partition("-agent")
    if sep and head in PLATFORMS:
        return head
    return None


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value if isinstance(value, str) else None


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` so an absent value stays absent in the projected file."""

    return {k: v for k, v in mapping.items() if v is not None}


class LearningStore:
    """``config.learning_settings`` … ``config.run_state_records``."""

    def __init__(self, db: ConfigDatabase) -> None:
        self._db = db

    # --- Settings ----------------------------------------------------------

    async def settings(self, client_slug: str, platform: str) -> dict[str, Any]:
        row = await self._db.fetchrow(
            "select * from learning_settings where client_slug = $1 and platform = $2",
            client_slug,
            platform,
        )
        if row is None:
            return {"antiRepeatDays": 30, "feedbackRows": 20, "sector": None}
        return {
            "antiRepeatDays": row["anti_repeat_days"],
            "feedbackRows": row["feedback_rows"],
            "sector": row["sector"],
        }

    async def put_settings(
        self,
        client_slug: str,
        platform: str,
        *,
        anti_repeat_days: int | None,
        feedback_rows: int | None,
        sector: str | None,
    ) -> dict[str, Any]:
        await self._db.execute(
            """
            insert into learning_settings (client_slug, platform, anti_repeat_days,
                                           feedback_rows, sector)
            values ($1, $2, coalesce($3, 30), coalesce($4, 20), $5)
            on conflict (client_slug, platform) do update
               set anti_repeat_days = coalesce($3, learning_settings.anti_repeat_days),
                   feedback_rows    = coalesce($4, learning_settings.feedback_rows),
                   sector           = coalesce($5, learning_settings.sector)
            """,
            client_slug,
            platform,
            anti_repeat_days,
            feedback_rows,
            sector,
        )
        return await self.settings(client_slug, platform)

    # --- Subject rows (B1) --------------------------------------------------

    async def subject_window(
        self, client_slug: str, platform: str, *, days: int
    ) -> list[dict[str, Any]]:
        """C7 §2.2 rows: everything drafted inside the window, newest first."""

        rows = await self._db.fetch(
            """
            select * from subject_rows
             where client_slug = $1 and platform = $2
               and drafted_at >= now() - make_interval(days => $3)
             order by drafted_at desc, id
            """,
            client_slug,
            platform,
            days,
        )
        return [_subject_row(r) for r in rows]

    async def subject_rows(
        self, client_slug: str, platform: str | None, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            select * from subject_rows
             where client_slug = $1 and ($2::text is null or platform = $2)
             order by drafted_at desc, id
             limit $3 offset $4
            """,
            client_slug,
            platform,
            limit,
            offset,
        )
        return [_subject_row(r) for r in rows]

    async def upsert_subject_row(
        self,
        connection: asyncpg.Connection,
        *,
        client_slug: str,
        platform: str,
        run_id: str,
        product_id: str | None,
        subject: dict[str, Any],
        deliverable: dict[str, Any],
        drafted_at: datetime | None,
    ) -> str | None:
        """One row per (run, subject); a collected-again run rewrites, not adds.

        ``status`` is NOT overwritten on conflict: the run wrote ``drafted``,
        and the client may already have moved the row to ``posted`` through
        the feedback route before the reconcile that collects the run lands.
        """

        text = subject.get("subject")
        stage = subject.get("stage") or deliverable.get("goal")
        if not isinstance(text, str) or not text.strip() or stage not in FUNNEL_STAGES:
            return None
        sources = deliverable.get("sources")
        row_id = await connection.fetchval(
            """
            insert into subject_rows (
                client_slug, platform, run_id, product_id,
                subject, angle, type, stage, goal, status, asset_kind, strategy_row_id,
                audience, why_now, sources, drafted_at
            ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                      coalesce($16, now()))
            on conflict (client_slug, platform, run_id, subject) do update
               set angle = excluded.angle, type = excluded.type, stage = excluded.stage,
                   goal = excluded.goal, asset_kind = excluded.asset_kind,
                   strategy_row_id = excluded.strategy_row_id,
                   audience = excluded.audience, why_now = excluded.why_now,
                   sources = excluded.sources, product_id = excluded.product_id
            returning id
            """,
            client_slug,
            platform,
            run_id,
            product_id,
            text.strip(),
            _text_or_none(subject.get("angle")),
            _text_or_none(subject.get("type") or deliverable.get("type")),
            stage,
            _text_or_none(subject.get("goal")),
            subject.get("status") if subject.get("status") in SUBJECT_STATUSES else "drafted",
            _text_or_none(subject.get("assetKind") or deliverable.get("kind")),
            _text_or_none(subject.get("strategyRowId")),
            _text_or_none(deliverable.get("audience")),
            _text_or_none(deliverable.get("whyNow")),
            [s for s in sources if isinstance(s, str)] if isinstance(sources, list) else [],
            drafted_at,
        )
        return str(row_id) if row_id is not None else None

    async def set_subject_status(
        self,
        connection: asyncpg.Connection,
        *,
        client_slug: str,
        platform: str,
        run_id: str,
        status: str,
        at: datetime,
    ) -> int:
        result = await connection.execute(
            """
            update subject_rows
               set status = $4,
                   posted_at = case when $4 = 'posted' then $5 else posted_at end
             where client_slug = $1 and platform = $2 and run_id = $3
            """,
            client_slug,
            platform,
            run_id,
            status,
            at,
        )
        return _rowcount(result)

    # --- Feedback log (B2) --------------------------------------------------

    async def recent_feedback(
        self, client_slug: str, platform: str, *, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            select * from client_feedback_log
             where client_slug = $1 and platform = $2
             order by at desc, id
             limit $3
            """,
            client_slug,
            platform,
            limit,
        )
        return [_feedback_row(r) for r in rows]

    async def append_feedback(
        self,
        connection: asyncpg.Connection,
        *,
        client_slug: str,
        platform: str,
        run_id: str | None,
        account: str | None,
        action: str,
        reason: str | None,
        original_text: str | None,
        final_text: str | None,
        actor: str | None,
        at: datetime | None,
        source: str,
        source_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one row; ``None`` when ``source_id`` was already imported."""

        row = await connection.fetchrow(
            """
            insert into client_feedback_log (
                client_slug, platform, run_id, account, action, reason,
                original_text, final_text, actor, at, source, source_id
            ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, coalesce($10, now()), $11, $12)
            on conflict (source_id) do nothing
            returning *
            """,
            client_slug,
            platform,
            run_id,
            account,
            action,
            reason,
            original_text,
            final_text,
            actor,
            at,
            source,
            source_id,
        )
        return _feedback_row(row) if row is not None else None

    # --- Preferences (B2, derived) ------------------------------------------

    async def preferences(self, client_slug: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "select * from client_preferences where client_slug = $1", client_slug
        )
        return _preferences_row(row) if row is not None else None

    async def put_preferences(
        self,
        client_slug: str,
        *,
        never_topics: list[str] | None,
        standing_instructions: list[str] | None,
        updated_by: str | None,
        formats: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """The fields a person sets. Derived fields are left alone."""

        await self._db.execute(
            """
            insert into client_preferences (client_slug, never_topics, standing_instructions,
                                            updated_by)
            values ($1, coalesce($2::text[], '{}'), coalesce($3::text[], '{}'), $4)
            on conflict (client_slug) do update
               set never_topics = coalesce($2::text[], client_preferences.never_topics),
                   standing_instructions =
                       coalesce($3::text[], client_preferences.standing_instructions),
                   updated_by = $4
            """,
            client_slug,
            never_topics,
            standing_instructions,
            updated_by,
        )
        if formats is not None:
            # Its own statement, so a database that has not taken 0008 yet
            # still accepts every write that does not carry `formats`.
            await self._db.execute(
                "update client_preferences set format_preferences = $2::jsonb"
                " where client_slug = $1",
                client_slug,
                formats,
            )
        prefs = await self.preferences(client_slug)
        assert prefs is not None
        return prefs

    async def derive_preferences(
        self, connection: asyncpg.Connection, client_slug: str
    ) -> dict[str, Any]:
        """Rebuild the derived half from the log and the collected records.

        Voice notes come from four places now (B2, SCRUM-494). Two are STATED --
        the review-cycle notes a run recorded (``voiceNotes`` on its C7 record)
        and ``note`` / ``change_requested`` reasons in the feedback log -- and
        two are COUNTED, which is what this ticket was missing:

        * words the client removes from draft after draft and never publishes
        * a consistent direction of length change across several edits
        * the same reason given for skipping several drafts

        The counted half lives in ``voice_lessons.py``, as pure functions over
        the texts, with its own reasoning about why none of it is a model call.
        The stated half is unchanged and wins a tie: a lesson somebody wrote in
        words is not replaced by one derived from a diff that says the same
        thing.

        A like is still never a voice note. It is now a LIKE: ``likes`` has been
        a column with no writer since this table was created, and what fills it
        is a ``posted`` action -- the client putting our sentences out under
        their own name without changing a word, which is the strongest
        endorsement the log actually contains.
        """

        # A ``change_requested`` reason is STATED, like a note: a person wrote,
        # in words, what the draft should have been. Counting it the way skip
        # reasons are counted (the same sentence twice) would almost never fire
        # on free text, and the same sentence typed at the engine's review gate
        # has become a voice note through the run record since C7 -- so a
        # request sent from a draft was the one place it was forgotten.
        note_rows = await connection.fetch(
            """
            select run_id, reason, at from client_feedback_log
             where client_slug = $1 and action in ('note', 'change_requested')
               and reason is not null
             order by at desc limit 50
            """,
            client_slug,
        )
        record_rows = await connection.fetch(
            """
            select run_id, record from run_state_records
             where client_slug = $1 order by collected_at desc limit 100
            """,
            client_slug,
        )
        count = await connection.fetchval(
            "select count(*) from client_feedback_log where client_slug = $1", client_slug
        )

        voice: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(lesson: Any, run_id: Any) -> None:
            if not isinstance(lesson, str) or not lesson.strip():
                return
            key = lesson.strip().lower()
            if key in seen:
                return
            seen.add(key)
            voice.append(_clean({"lesson": lesson.strip(), "fromRunId": run_id}))

        for row in record_rows:
            record = row["record"] if isinstance(row["record"], dict) else {}
            notes = record.get("voiceNotes")
            if isinstance(notes, list):
                for note in notes:
                    if isinstance(note, dict):
                        add(note.get("lesson"), row["run_id"])
        for row in note_rows:
            add(row["reason"], row["run_id"])

        # The counted half. Each query is bounded and ordered newest-first: a
        # client with three years of history derives from their recent voice,
        # not from how they wrote when they signed up.
        edit_rows = await connection.fetch(
            """
            select original_text, final_text from client_feedback_log
             where client_slug = $1 and action = 'posted_with_edits'
               and original_text is not null and final_text is not null
             order by at desc limit 50
            """,
            client_slug,
        )
        skip_rows = await connection.fetch(
            """
            select reason from client_feedback_log
             where client_slug = $1 and action = 'skipped' and reason is not null
             order by at desc limit 50
            """,
            client_slug,
        )
        # `posted`, not `posted_with_edits`: the endorsement is that nothing was
        # changed. Joined to the subject row so a like carries the post it refers
        # to rather than a run id nobody can read, and LEFT joined so a like
        # survives a subject row that was never written (a pre-C7 run).
        like_rows = await connection.fetch(
            """
            select f.run_id, f.at, s.subject
              from client_feedback_log f
              left join subject_rows s
                on s.run_id = f.run_id and s.platform = f.platform
             where f.client_slug = $1 and f.action = 'posted' and f.run_id is not null
             order by f.at desc limit 20
            """,
            client_slug,
        )

        derived_voice = derive_voice_notes(
            carried=voice,
            edit_pairs=[(r["original_text"], r["final_text"]) for r in edit_rows],
            skip_reasons=[r["reason"] for r in skip_rows],
        )
        likes = likes_from_posts(
            [
                {"runId": r["run_id"], "subject": r["subject"], "at": _iso(r["at"])}
                for r in like_rows
            ]
        )

        await connection.execute(
            """
            insert into client_preferences (client_slug, voice_notes, likes, derived_at,
                                            derived_from_count)
            values ($1, $2::jsonb, $3::jsonb, now(), $4)
            on conflict (client_slug) do update
               set voice_notes = excluded.voice_notes,
                   likes = excluded.likes,
                   derived_at = now(),
                   derived_from_count = excluded.derived_from_count
             -- Same derivation, same row: `derivedAt` moves only when the result does.
             where client_preferences.voice_notes is distinct from excluded.voice_notes
                or client_preferences.likes is distinct from excluded.likes
                or client_preferences.derived_from_count
                   is distinct from excluded.derived_from_count
            """,
            client_slug,
            derived_voice,
            likes,
            int(count or 0),
        )
        row = await connection.fetchrow(
            "select * from client_preferences where client_slug = $1", client_slug
        )
        assert row is not None
        return _preferences_row(row)

    # --- Platform state (C7 §2.1 / §3) ---------------------------------------

    async def platform_state(self, client_slug: str, platform: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "select * from platform_state where client_slug = $1 and platform = $2",
            client_slug,
            platform,
        )
        if row is None:
            return None
        state = row["state"] if isinstance(row["state"], dict) else {}
        return {**state, "lastUpdated": state.get("lastUpdated") or _iso(row["collected_at"])}

    async def upsert_platform_state(
        self,
        connection: asyncpg.Connection,
        *,
        client_slug: str,
        platform: str,
        state: dict[str, Any],
        run_id: str | None,
    ) -> None:
        await connection.execute(
            """
            insert into platform_state (client_slug, platform, state, last_run_id, collected_at)
            values ($1, $2, $3::jsonb, $4, now())
            on conflict (client_slug, platform) do update
               set state = excluded.state, last_run_id = excluded.last_run_id,
                   collected_at = now()
             -- An identical state is not a change: `lastUpdated` in the projected
             -- file comes from this row, and churning it would make every collect
             -- look like news and defeat the projector's content-hash no-op.
             where platform_state.state is distinct from excluded.state
            """,
            client_slug,
            platform,
            state,
            run_id,
        )

    # --- Strategy map (C1) --------------------------------------------------

    async def strategy_map(self, client_slug: str, platform: str) -> dict[str, Any] | None:
        head = await self._db.fetchrow(
            "select * from strategy_maps where client_slug = $1 and platform = $2",
            client_slug,
            platform,
        )
        if head is None:
            return None
        rows = await self._db.fetch(
            """
            select * from strategy_map_rows
             where client_slug = $1 and platform = $2
             order by position, row_id
            """,
            client_slug,
            platform,
        )
        return {
            "platform": platform,
            "builtAt": _iso(head["built_at"]),
            "source": head["source"],
            "audience": head["audience"] if isinstance(head["audience"], list) else [],
            "defaultMix": head["default_mix"] if isinstance(head["default_mix"], dict) else {},
            "rows": [
                _clean(
                    {
                        "id": r["row_id"],
                        "problem": r["problem"],
                        "stage": r["stage"],
                        "idea": r["idea"],
                        "type": r["type"],
                        "evidence": r["evidence"],
                        "status": r["status"],
                        "usedByRunId": r["used_by_run_id"],
                    }
                )
                for r in rows
            ],
        }

    async def put_strategy_map(
        self,
        client_slug: str,
        platform: str,
        *,
        source: str,
        built_at: datetime | None,
        audience: list[Any],
        default_mix: dict[str, Any] | None,
        rows: list[dict[str, Any]],
        replace: bool,
    ) -> dict[str, Any]:
        """Upsert the header and the rows; ``replace`` retires rows not named."""

        async with self._db.transaction() as connection:
            await connection.execute(
                """
                insert into strategy_maps (client_slug, platform, source, built_at, audience,
                                           default_mix)
                values ($1, $2, $3, coalesce($4, now()), $5::jsonb,
                        coalesce($6::jsonb, '{"attention": 3, "expertise": 2, "decide": 1}'))
                on conflict (client_slug, platform) do update
                   set source = excluded.source, built_at = excluded.built_at,
                       audience = excluded.audience,
                       default_mix = coalesce($6::jsonb, strategy_maps.default_mix)
                """,
                client_slug,
                platform,
                source,
                built_at,
                audience,
                default_mix,
            )
            named: list[str] = []
            for position, row in enumerate(rows):
                row_id = row.get("id")
                idea = row.get("idea")
                stage = row.get("stage")
                if not isinstance(row_id, str) or not isinstance(idea, str) or not idea.strip():
                    continue
                if stage not in FUNNEL_STAGES:
                    continue
                named.append(row_id)
                await connection.execute(
                    """
                    insert into strategy_map_rows (client_slug, platform, row_id, problem, stage,
                                                   idea, type, evidence, status, position)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, coalesce($9, 'open'), $10)
                    on conflict (client_slug, platform, row_id) do update
                       set problem = excluded.problem, stage = excluded.stage,
                           idea = excluded.idea, type = excluded.type,
                           evidence = excluded.evidence, position = excluded.position,
                           -- a row a run already took stays taken
                           status = case when strategy_map_rows.status = 'used'
                                         then 'used' else excluded.status end
                    """,
                    client_slug,
                    platform,
                    row_id,
                    _text_or_none(row.get("problem")),
                    stage,
                    idea.strip(),
                    _text_or_none(row.get("type")),
                    _text_or_none(row.get("evidence")),
                    row.get("status") if row.get("status") in ("open", "used", "retired") else None,
                    position,
                )
            if replace:
                await connection.execute(
                    """
                    update strategy_map_rows set status = 'retired'
                     where client_slug = $1 and platform = $2
                       and status = 'open' and not (row_id = any($3::text[]))
                    """,
                    client_slug,
                    platform,
                    named,
                )
        result = await self.strategy_map(client_slug, platform)
        assert result is not None
        return result

    async def mark_strategy_row_used(
        self,
        connection: asyncpg.Connection,
        *,
        client_slug: str,
        platform: str,
        row_id: str,
        run_id: str,
    ) -> None:
        await connection.execute(
            """
            update strategy_map_rows
               set status = 'used', used_by_run_id = coalesce(used_by_run_id, $4)
             where client_slug = $1 and platform = $2 and row_id = $3 and status <> 'retired'
            """,
            client_slug,
            platform,
            row_id,
            run_id,
        )

    # --- Craft rules (D41) --------------------------------------------------

    async def craft_rules(
        self, client_slug: str, platform: str, *, sector: str | None
    ) -> list[dict[str, Any]]:
        """Active L1 for the platform, L2 for the sector, L3 for the client."""

        rows = await self._db.fetch(
            """
            select * from craft_rules
             where platform = $1 and status = 'active'
               and (layer = 'L1'
                    or (layer = 'L2' and sector = $2)
                    or (layer = 'L3' and client_slug = $3))
             order by layer, id
            """,
            platform,
            sector,
            client_slug,
        )
        return [
            {
                "id": r["id"],
                "layer": r["layer"],
                "kind": r["kind"],
                "rule": r["rule"],
                "why": r["why"],
                "metric": r["metric"],
                "sampleSize": r["sample_size"],
                "overrides": list(r["overrides"] or []),
            }
            for r in rows
        ]

    async def put_craft_rules(self, rules: list[dict[str, Any]], *, updated_by: str | None) -> int:
        async with self._db.transaction() as connection:
            for rule in rules:
                await connection.execute(
                    """
                    insert into craft_rules (id, platform, layer, sector, client_slug, kind,
                                             rule, why, metric, sample_size, overrides, status,
                                             updated_by)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    on conflict (id) do update
                       set platform = excluded.platform, layer = excluded.layer,
                           sector = excluded.sector, client_slug = excluded.client_slug,
                           kind = excluded.kind, rule = excluded.rule, why = excluded.why,
                           metric = excluded.metric, sample_size = excluded.sample_size,
                           overrides = excluded.overrides, status = excluded.status,
                           updated_by = excluded.updated_by
                    """,
                    rule["id"],
                    rule["platform"],
                    rule["layer"],
                    rule.get("sector"),
                    rule.get("clientSlug"),
                    rule.get("kind", "default"),
                    rule["rule"],
                    rule.get("why"),
                    rule.get("metric"),
                    rule.get("sampleSize"),
                    list(rule.get("overrides") or []),
                    rule.get("status", "active"),
                    updated_by,
                )
        return len(rules)

    # --- Collected records ----------------------------------------------------

    async def upsert_run_record(
        self,
        connection: asyncpg.Connection,
        *,
        run_id: str,
        client_slug: str,
        platform: str,
        product_id: str | None,
        record: dict[str, Any],
        content_hash: str,
        collected_by: str | None,
    ) -> bool:
        """``True`` when the record is new or changed, ``False`` when identical."""

        previous = await connection.fetchval(
            "select content_hash from run_state_records where run_id = $1", run_id
        )
        if previous == content_hash:
            return False
        await connection.execute(
            """
            insert into run_state_records (run_id, client_slug, platform, product_id, record,
                                           content_hash, collected_by)
            values ($1, $2, $3, $4, $5::jsonb, $6, $7)
            on conflict (run_id) do update
               set record = excluded.record, content_hash = excluded.content_hash,
                   collected_at = now(), collected_by = excluded.collected_by,
                   product_id = excluded.product_id
            """,
            run_id,
            client_slug,
            platform,
            product_id,
            record,
            content_hash,
            collected_by,
        )
        return True

    async def run_record(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("select * from run_state_records where run_id = $1", run_id)
        if row is None:
            return None
        return {
            "runId": row["run_id"],
            "clientSlug": row["client_slug"],
            "platform": row["platform"],
            "productId": row["product_id"],
            "record": row["record"],
            "contentHash": row["content_hash"],
            "collectedAt": _iso(row["collected_at"]),
            "collectedBy": row["collected_by"],
        }

    @property
    def db(self) -> ConfigDatabase:
        return self._db


# --- Row shapes ---------------------------------------------------------------


def _subject_row(r: asyncpg.Record) -> dict[str, Any]:
    return _clean(
        {
            "id": str(r["id"]),
            "runId": r["run_id"],
            "subject": r["subject"],
            "angle": r["angle"],
            "type": r["type"],
            "stage": r["stage"],
            "goal": r["goal"],
            "status": r["status"],
            "assetKind": r["asset_kind"],
            "strategyRowId": r["strategy_row_id"],
            "audience": r["audience"],
            "whyNow": r["why_now"],
            "draftedAt": _iso(r["drafted_at"]),
            "postedAt": _iso(r["posted_at"]),
        }
    )


def _feedback_row(r: asyncpg.Record) -> dict[str, Any]:
    return _clean(
        {
            "id": str(r["id"]),
            "runId": r["run_id"],
            "account": r["account"],
            "action": r["action"],
            "reason": r["reason"],
            "originalText": r["original_text"],
            "finalText": r["final_text"],
            "actor": r["actor"],
            "at": _iso(r["at"]),
            "source": r["source"],
        }
    )


def _preferences_row(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "neverTopics": list(r["never_topics"] or []),
        "standingInstructions": list(r["standing_instructions"] or []),
        "likes": r["likes"] if isinstance(r["likes"], list) else [],
        "voiceNotes": r["voice_notes"] if isinstance(r["voice_notes"], list) else [],
        "derivedAt": _iso(r["derived_at"]),
        "derivedFromCount": r["derived_from_count"],
        # 0008. Read defensively: a row from a database without the column,
        # or a value the codec returned as text, is simply no preference.
        "formats": _formats_of(dict(r).get("format_preferences")),
    }


def _formats_of(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _rowcount(result: str) -> int:
    """``UPDATE 3`` → 3. asyncpg returns the command tag as a string."""

    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


__all__ = [
    "FEEDBACK_ACTIONS",
    "FUNNEL_STAGES",
    "PLATFORMS",
    "PRODUCT_PLATFORM_OVERRIDES",
    "STATUS_FOR_ACTION",
    "SUBJECT_STATUSES",
    "LearningStore",
    "platform_for_product",
]
