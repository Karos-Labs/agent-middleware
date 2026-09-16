-- =====================================================================
-- 0007_learning_loop.sql — what the platform learns about a client, per
--                          account, lives here (O09; SCRUM-461/462/463)
--
-- Build plan 04 items B1 (the subject table), B2 (the unified feedback log
-- and the preferences derived from it) and the stores behind C7's projected
-- files (`docs/contracts/C7-run-context.md`): the per-platform introduction
-- doc, the strategy map with a funnel stage on every row, and the three
-- layers of craft rules.
--
-- ## Why Postgres and not Firestore or GCS (O09)
--
-- Every question the loop asks is a window or a join: "which subjects did we
-- post on X in the last 30 days", "what did the client do with the last
-- twenty drafts on this account", "which open strategy row at this stage is
-- not already in the window". Firestore answers none of them without an
-- index per question and a client-side merge, and a JSON file in the bucket
-- is a snapshot with no concurrency story once two runs land in the same
-- hour. The configuration database already exists, is already the place a
-- run's verdict moved to (0006), and a learning row is the same kind of
-- thing: low volume, joinable, history that must not be rewritten.
--
-- The ENGINE never reads any of this. The middleware projects it into the
-- workspace as files before a dispatch, and collects the run's state files
-- back into these tables afterwards. Drop every table here and the engine
-- runs exactly as it did before C7 -- with nothing projected.
--
-- ## The vocabulary is shared, not translated (C7 §4.5)
--
-- `stage` is the engine's FUNNEL_STAGES; `action` is the five feedback
-- actions the engine's prompt renderer reads; `status` on a subject row is
-- the portal's review outcome. Enforced as CHECKs so a typo in either
-- repository is refused by the database rather than silently projected.
-- =====================================================================

begin;

set local search_path = config, public;

-- --------------------------------------------------------------------
-- learning_settings — per client × platform knobs the projector reads
-- --------------------------------------------------------------------

create table if not exists config.learning_settings (
    client_slug         text        not null,
    platform            text        not null,
    -- C7 §2.2: a subject inside this window is not proposed again.
    anti_repeat_days    integer     not null default 30,
    -- How many recent feedback rows the run is shown (C7 §2.3).
    feedback_rows       integer     not null default 20,
    -- Which L2 craft overlay applies (`craft_rules.sector`). Null: none.
    sector              text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    primary key (client_slug, platform),
    constraint learning_settings_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint learning_settings_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint learning_settings_windows_positive
        check (anti_repeat_days between 1 and 365 and feedback_rows between 1 and 200)
);

drop trigger if exists learning_settings_90_touch on config.learning_settings;
create trigger learning_settings_90_touch before update on config.learning_settings
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- subject_rows — B1: every subject the platform drafted, with its goal
-- --------------------------------------------------------------------
--
-- One row per deliverable the engine produced (C7 §3.1 `subjectRow`),
-- upserted by `collect` on (client, platform, run, subject) so a resumed
-- run lands once. The portal moves `status` as the client reviews; the
-- projector reads the rows inside `anti_repeat_days` as the subject window.

create table if not exists config.subject_rows (
    id                  uuid        primary key default gen_random_uuid(),
    client_slug         text        not null,
    platform            text        not null,
    run_id              text        not null,
    product_id          text,

    subject             text        not null,
    angle               text,
    type                text,
    stage               text        not null,
    goal                text,
    status              text        not null default 'drafted',
    asset_kind          text,
    strategy_row_id     text,

    -- C3 / D11: the goal line the run wrote for this deliverable.
    audience            text,
    why_now             text,
    sources             text[]      not null default '{}',

    drafted_at          timestamptz not null default now(),
    posted_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    unique (client_slug, platform, run_id, subject),
    constraint subject_rows_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint subject_rows_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint subject_rows_stage_vocabulary
        check (stage in ('attention', 'expertise', 'decide')),
    constraint subject_rows_status_vocabulary
        check (status in ('drafted', 'approved', 'posted', 'skipped', 'change_requested')),
    constraint subject_rows_subject_not_blank
        check (length(btrim(subject)) > 0)
);

create index if not exists subject_rows_window_idx
    on config.subject_rows (client_slug, platform, drafted_at desc);
create index if not exists subject_rows_run_idx
    on config.subject_rows (run_id);

drop trigger if exists subject_rows_90_touch on config.subject_rows;
create trigger subject_rows_90_touch before update on config.subject_rows
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- client_feedback_log — B2: what the client did with a draft, per account
-- --------------------------------------------------------------------
--
-- Unified: the portal's review actions (posted / posted_with_edits /
-- skipped / change_requested) and free notes land in one table, so "what
-- has this client been telling us" is one query rather than four
-- collections. Append-only like 0006: an action, once taken, is history.

create table if not exists config.client_feedback_log (
    id                  uuid        primary key default gen_random_uuid(),
    client_slug         text        not null,
    platform            text        not null,
    run_id              text,
    account             text,

    action              text        not null,
    reason              text,
    original_text       text,
    final_text          text,
    actor               text,
    at                  timestamptz not null default now(),

    -- Where the row came from: the portal's review UI, an import, or the
    -- collector reading a run's review notes.
    source              text        not null default 'portal',
    source_id           text        unique,
    created_at          timestamptz not null default now(),

    constraint client_feedback_log_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint client_feedback_log_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint client_feedback_log_action_vocabulary
        check (action in ('posted', 'posted_with_edits', 'skipped', 'change_requested', 'note')),
    constraint client_feedback_log_source_vocabulary
        check (source in ('portal', 'import', 'collect')),
    -- An edit is a pair; half a pair teaches nothing.
    constraint client_feedback_log_edit_is_paired
        check (action <> 'posted_with_edits'
               or (original_text is not null and final_text is not null))
);

create index if not exists client_feedback_log_recent_idx
    on config.client_feedback_log (client_slug, platform, at desc);
create index if not exists client_feedback_log_run_idx
    on config.client_feedback_log (run_id) where run_id is not null;

create or replace function config.guard_client_feedback_log_history() returns trigger
language plpgsql as $$
begin
    raise exception
        'client_feedback_log: row % is history and cannot be % ',
        old.id, lower(tg_op)
        using errcode = 'restrict_violation',
              hint = 'A wrong action is answered by another row, not by changing this one.';
end;
$$;

drop trigger if exists client_feedback_log_00_guard on config.client_feedback_log;
create trigger client_feedback_log_00_guard
    before update or delete on config.client_feedback_log
    for each row execute function config.guard_client_feedback_log_history();

-- --------------------------------------------------------------------
-- client_preferences — B2: the derived, client-wide view (C7 §2.4)
-- --------------------------------------------------------------------
--
-- One row per client. `never_topics` and `standing_instructions` are set by
-- a person in the portal; `voice_notes` and `likes` are derived -- voice
-- notes only from edits and review revisions (Craft 11 §3), never from a
-- like alone -- and rewritten by the derivation, so they carry no history.

create table if not exists config.client_preferences (
    client_slug             text        primary key,
    never_topics            text[]      not null default '{}',
    standing_instructions   text[]      not null default '{}',
    likes                   jsonb       not null default '[]'::jsonb,
    voice_notes             jsonb       not null default '[]'::jsonb,
    derived_at              timestamptz,
    derived_from_count      integer     not null default 0,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    updated_by              text,

    constraint client_preferences_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint client_preferences_arrays_are_arrays
        check (jsonb_typeof(likes) = 'array' and jsonb_typeof(voice_notes) = 'array')
);

drop trigger if exists client_preferences_90_touch on config.client_preferences;
create trigger client_preferences_90_touch before update on config.client_preferences
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- platform_state — the introduction doc per account (C7 §2.1 / §3)
-- --------------------------------------------------------------------
--
-- Kept as one jsonb document rather than columns: its shape is the engine's
-- (`state/<platform>/platform-state.json`), it is collected whole and
-- projected whole, and nothing here queries inside it.

create table if not exists config.platform_state (
    client_slug         text        not null,
    platform            text        not null,
    state               jsonb       not null default '{}'::jsonb,
    last_run_id         text,
    collected_at        timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    primary key (client_slug, platform),
    constraint platform_state_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint platform_state_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint platform_state_is_object
        check (jsonb_typeof(state) = 'object')
);

drop trigger if exists platform_state_90_touch on config.platform_state;
create trigger platform_state_90_touch before update on config.platform_state
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- strategy_maps / strategy_map_rows — C1: the topic pool with goals
-- --------------------------------------------------------------------

create table if not exists config.strategy_maps (
    client_slug         text        not null,
    platform            text        not null,
    built_at            timestamptz not null default now(),
    source              text        not null default 'manual',
    audience            jsonb       not null default '[]'::jsonb,
    -- D32: the default 3/2/1 mix of stages a week of runs should show.
    default_mix         jsonb       not null default '{"attention": 3, "expertise": 2, "decide": 1}'::jsonb,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    primary key (client_slug, platform),
    constraint strategy_maps_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint strategy_maps_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint strategy_maps_source_vocabulary
        check (source in ('setup-run', 'first-run', 'manual'))
);

drop trigger if exists strategy_maps_90_touch on config.strategy_maps;
create trigger strategy_maps_90_touch before update on config.strategy_maps
    for each row execute function config.touch_updated_at();

create table if not exists config.strategy_map_rows (
    client_slug         text        not null,
    platform            text        not null,
    row_id              text        not null,
    problem             text,
    stage               text        not null,
    idea                text        not null,
    type                text,
    evidence            text,
    status              text        not null default 'open',
    -- Set by `collect` when a run took this row (C7 §3.1 strategyRowId).
    used_by_run_id      text,
    position            integer     not null default 0,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    primary key (client_slug, platform, row_id),
    foreign key (client_slug, platform)
        references config.strategy_maps (client_slug, platform) on delete cascade,
    constraint strategy_map_rows_stage_vocabulary
        check (stage in ('attention', 'expertise', 'decide')),
    constraint strategy_map_rows_status_vocabulary
        check (status in ('open', 'used', 'retired')),
    constraint strategy_map_rows_idea_not_blank
        check (length(btrim(idea)) > 0)
);

drop trigger if exists strategy_map_rows_90_touch on config.strategy_map_rows;
create trigger strategy_map_rows_90_touch before update on config.strategy_map_rows
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- craft_rules — D41: the three layers, resolved by the projector
-- --------------------------------------------------------------------
--
-- L1 is the platform's base (scope null), L2 a sector overlay (`sector`),
-- L3 a client's own learned rules (`client_slug`). Exactly one scope column
-- matches the layer, enforced below. `kind = 'hard'` is allowed on L1 only:
-- a hard rule always wins (C7 §2.7), so a lower layer cannot declare one.
-- `overrides` names the ids a rule beats when the two conflict; the
-- projector refuses a pair whose loser is hard.

create table if not exists config.craft_rules (
    id                  text        primary key,
    platform            text        not null,
    layer               text        not null,
    sector              text,
    client_slug         text,
    kind                text        not null default 'default',
    rule                text        not null,
    why                 text,
    metric              text,
    sample_size         integer,
    overrides           text[]      not null default '{}',
    status              text        not null default 'active',
    since               timestamptz not null default now(),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    updated_by          text,

    constraint craft_rules_platform_vocabulary
        check (platform in ('x', 'linkedin', 'reddit', 'instagram', 'tiktok')),
    constraint craft_rules_layer_vocabulary
        check (layer in ('L1', 'L2', 'L3')),
    constraint craft_rules_kind_vocabulary
        check (kind in ('hard', 'default')),
    constraint craft_rules_status_vocabulary
        check (status in ('active', 'retired')),
    constraint craft_rules_scope_matches_layer
        check ((layer = 'L1' and sector is null and client_slug is null)
            or (layer = 'L2' and sector is not null and client_slug is null)
            or (layer = 'L3' and sector is null and client_slug is not null)),
    constraint craft_rules_hard_is_l1_only
        check (kind = 'default' or layer = 'L1'),
    constraint craft_rules_rule_not_blank
        check (length(btrim(rule)) > 0)
);

create index if not exists craft_rules_scope_idx
    on config.craft_rules (platform, layer, sector, client_slug) where status = 'active';

drop trigger if exists craft_rules_90_touch on config.craft_rules;
create trigger craft_rules_90_touch before update on config.craft_rules
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- run_state_records — the collected C7 §3.1 record, verbatim
-- --------------------------------------------------------------------
--
-- Kept whole so a subject row can be re-derived if its mapping changes and
-- so "what did the run say it read" (`readiness`) survives. Upserted on
-- run_id: collecting twice is a no-op, which is what lets the portal call
-- collect on every reconcile without counting.

create table if not exists config.run_state_records (
    run_id              text        primary key,
    client_slug         text        not null,
    platform            text        not null,
    product_id          text,
    record              jsonb       not null,
    content_hash        text        not null,
    collected_at        timestamptz not null default now(),
    collected_by        text,

    constraint run_state_records_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint run_state_records_is_object
        check (jsonb_typeof(record) = 'object')
);

create index if not exists run_state_records_client_idx
    on config.run_state_records (client_slug, platform, collected_at desc);

comment on table config.subject_rows is
    'B1 (SCRUM-462): one row per deliverable the engine produced, with its funnel '
    'stage and goal. The rows inside anti_repeat_days are projected as the subject '
    'window (C7 §2.2); the portal moves status as the client reviews.';
comment on table config.client_feedback_log is
    'B2 (SCRUM-463): every review action and note a client gave, per account. '
    'Append-only. Projected as C7 §2.3 feedback and derived into client_preferences.';
comment on table config.client_preferences is
    'B2 (SCRUM-463): the client-wide preferences a run reads (C7 §2.4). '
    'never_topics/standing_instructions are set by a person; voice_notes/likes are derived.';
comment on table config.craft_rules is
    'D41: L1 base / L2 sector / L3 client craft rules. The projector merges the '
    'three for a client and resolves precedence; the run receives the result (C7 §2.7).';
comment on table config.run_state_records is
    'The C7 §3.1 record a run wrote to state/runs/<runId>.json, collected verbatim. '
    'Upserted on run_id, so collect is idempotent.';

insert into config.schema_migrations (filename) values ('0007_learning_loop.sql')
on conflict (filename) do nothing;

commit;
