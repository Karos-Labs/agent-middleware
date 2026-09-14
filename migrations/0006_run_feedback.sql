-- =====================================================================
-- 0006_run_feedback.sql — a reviewer's verdict on a run moves to Postgres
--                         (SCRUM-224 / S11)
--
-- Feedback on a RUN is a human judgement that may be promoted into a few-shot
-- example: low volume, and it has to be joinable to the prompt version it
-- criticised, because "which prompt produced the output this person rated 2"
-- is the whole reason to keep it. Firestore cannot join, so `run_feedback`
-- moves here. Feedback on a DRAFT (the portal's xDraftFeedback and friends)
-- is a different thing and stays where it is.
--
-- ## What is kept, and what is refused
--
-- The history must be preserved. A verdict, once given, is a fact about a
-- moment; editing it afterwards is how a training set quietly lies about how
-- good a prompt version was. So a row here is append-only EXCEPT for one
-- column: `promoted_example_id`, which records that the verdict was turned
-- into an example -- a fact about a later moment, not a change to the
-- earlier one. The guard below allows exactly that column (and
-- `updated_at`) to change, and nothing else, and refuses DELETE outright.
--
-- ## The join to the prompt version
--
-- A run records the engine's `prompt_id` and its pinned `prompt_version`
-- ("x-draft", "2"). S7 keeps every CONTENT version of that prompt in
-- `prompt_versions`, append-only, under the prompt whose `engine_prompt_id`
-- and `engine_version` match. So the version that was in the box when the
-- run happened is the newest one created at or before the run -- and that
-- is what `prompt_version_id` points at, resolved at write time.
--
-- It is NULLABLE, on purpose. Not every run names a prompt, and a prompt
-- that was never saved through S7 has no versions here. The raw engine ids
-- are kept beside the FK in `engine_prompt_id` / `engine_prompt_version` so
-- a verdict on such a run is still attributable, and a later backfill can
-- resolve the FK once the prompt has been saved through the store.
--
-- ## Two identities for the agent, deliberately
--
-- `agent_id` is the Firestore document id the middleware's API resolves an
-- agent by, and it is NOT NULL because every verdict arrives through that
-- API. `agent_slug` is the `config.agents` row, resolved through S5's
-- `source_id`, and it is nullable because not every agent has been imported
-- yet. The FK is what makes "all feedback on prompts of agent X" a join
-- rather than a string match; the id is what keeps the API's contract.
--
-- ## Reversible, like S5
--
-- `source_id` carries the Firestore feedback document id for rows brought
-- over by `scripts/import_run_feedback.py`, and is UNIQUE, so the import is
-- idempotent. Drop this table and the Firestore collection is exactly what
-- it was; nothing here is read by any run path.
-- =====================================================================

begin;

set local search_path = config, public;

create table if not exists config.run_feedback (
    id                      uuid        primary key default gen_random_uuid(),

    -- The run and the agent, as the middleware's API names them.
    run_id                  text        not null,
    agent_id                text        not null,
    agent_slug              config.slug references config.agents (slug) on delete restrict,
    client_slug             text,

    -- The verdict. Same vocabulary and bounds as app.core.enums.FeedbackStatus
    -- and the API's FeedbackCreate; enforced here so the database refuses a
    -- rating of 6 whatever the code above it does.
    rating                  smallint    not null,
    status                  text        not null,
    correction_notes        text,
    corrected_output        text,
    reviewer                text,
    tags                    text[]      not null default '{}',

    -- The prompt version this verdict is about (see the header).
    prompt_version_id       uuid        references config.prompt_versions (id) on delete restrict,
    engine_prompt_id        text,
    engine_prompt_version   text,

    -- Set once, when the verdict becomes a few-shot example. The example
    -- itself is still a Firestore document (PromptService.create_example),
    -- so this is its id there, not a FK.
    promoted_example_id     text,

    -- Provenance for rows imported from the Firestore collection.
    source_id               text        unique,
    imported_at             timestamptz,

    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),

    constraint run_feedback_rating_range
        check (rating between 1 and 5),
    constraint run_feedback_status_vocabulary
        check (status in ('approved', 'rejected', 'needs_changes')),
    constraint run_feedback_reviewer_length
        check (reviewer is null or length(reviewer) <= 255),
    constraint run_feedback_client_slug_charset
        check (client_slug is null
               or (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128)),
    -- The engine ids come as a pair or not at all; half a reference is a
    -- reference two readers will disagree about.
    constraint run_feedback_engine_prompt_is_paired
        check ((engine_prompt_id is null and engine_prompt_version is null)
               or (engine_prompt_id is not null and engine_prompt_version is not null))
);

-- The two access patterns the Firestore version needed a composite index
-- for, and the join this table exists for.
create index if not exists run_feedback_run_idx
    on config.run_feedback (run_id, created_at);
create index if not exists run_feedback_agent_rating_idx
    on config.run_feedback (agent_id, rating desc, created_at desc);
create index if not exists run_feedback_prompt_version_idx
    on config.run_feedback (prompt_version_id)
    where prompt_version_id is not null;

drop trigger if exists run_feedback_90_touch on config.run_feedback;
create trigger run_feedback_90_touch before update on config.run_feedback
    for each row execute function config.touch_updated_at();

-- --- The guard: a verdict is history; only its promotion may be recorded ---

create or replace function config.guard_run_feedback_history() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception
            'run_feedback: verdict % on run % is history and cannot be deleted',
            old.id, old.run_id
            using errcode = 'restrict_violation',
                  hint = 'A wrong verdict is answered by another verdict, not by removing this one.';
    end if;

    -- Compare the whole row minus the two columns that may move, so a column
    -- added by a later migration is frozen by default -- the same direction
    -- the agent_versions guard chose, and for the same reason.
    if (to_jsonb(old) - 'promoted_example_id' - 'updated_at')
       is distinct from
       (to_jsonb(new) - 'promoted_example_id' - 'updated_at') then
        raise exception
            'run_feedback: verdict % on run % is history; only promoted_example_id may change',
            old.id, old.run_id
            using errcode = 'restrict_violation';
    end if;

    -- Promotion is one-way: once a verdict has become an example, it does not
    -- become a different example, and it does not un-become one.
    if old.promoted_example_id is not null
       and new.promoted_example_id is distinct from old.promoted_example_id then
        raise exception
            'run_feedback: verdict % was already promoted to example %',
            old.id, old.promoted_example_id
            using errcode = 'restrict_violation';
    end if;

    return new;
end;
$$;

drop trigger if exists run_feedback_00_guard on config.run_feedback;
create trigger run_feedback_00_guard
    before update or delete on config.run_feedback
    for each row execute function config.guard_run_feedback_history();

comment on table config.run_feedback is
    'A reviewer''s verdict on one run (S11). Append-only except '
    'promoted_example_id, which is set once. Joinable to the prompt version '
    'the run was produced with through prompt_version_id.';
comment on column config.run_feedback.prompt_version_id is
    'The newest prompt_versions row of the run''s engine prompt created at or '
    'before the run. Null when the run named no prompt or the prompt has no '
    'versions here yet; engine_prompt_id / engine_prompt_version keep the raw '
    'reference either way.';

insert into config.schema_migrations (filename) values ('0006_run_feedback.sql')
on conflict (filename) do nothing;

commit;
