-- =====================================================================
-- 0004_prompt_projection.sql — the prompt store's link to what runs
--                              (SCRUM-221 / S7)
--
-- S7 makes `config.prompt_versions` the authority for prompt text and keeps
-- agent-engine's own documents as a WRITTEN-THROUGH PROJECTION, so no run path
-- changes. That needs four columns: which engine document a prompt projects
-- to, and which of its versions is currently sitting there.
--
-- ## Why a projection and not a cutover
--
-- A stage's `skillRef` is pinned in compiled TypeScript -- `skillRef:
-- "x-draft@2"` -- so the engine loads exactly that document and no other.
-- Creating a `@3` would be INERT: nothing would load it until somebody changed
-- TypeScript and redeployed. That is why the endpoint Studio has been writing
-- to since 24.08 overwrites content in place, and it is not laziness; it is
-- the only write that takes effect.
--
-- So the fix cannot be "stop overwriting". It is: keep every version forever
-- HERE, append-only, and write the current one THERE, into the document the
-- pinned skillRef names. The engine sees exactly what it saw before.
--
-- This is also what makes it reversible, which is the same argument S5 makes
-- for its import: delete these rows and the Firestore documents are still the
-- source of truth.
--
-- ## Two numbering schemes, deliberately not conflated
--
-- `engine_version` is the number inside the pinned skillRef ("2"). It is a
-- STRING because the engine treats it as one, and it does not increment when a
-- prompt is edited.
--
-- `prompt_versions.version` is the number of the CONTENT -- 1, 2, 3, ... and
-- one higher on every save. Conflating the two is how a restore overwrites the
-- wrong thing: "restore version 2" would be ambiguous between "the second
-- thing we wrote" and "the document the stage is pinned to".
-- =====================================================================

begin;

set local search_path = config, public;

alter table config.prompts
    -- The engine document this prompt projects into. Null on a prompt that
    -- exists only here -- which is every prompt authored through the
    -- Configuration API for a version that has not been published yet.
    add column if not exists engine_prompt_id text,
    add column if not exists engine_version   text,
    -- Which of this prompt's versions is currently sitting in that document.
    -- Not the same as "the newest version": a save that failed half-way
    -- through its projection leaves a version here that is not live there, and
    -- this column is how that is visible rather than assumed.
    add column if not exists projected_version integer,
    add column if not exists projected_at      timestamptz;


-- One prompt per engine document. Two prompt keys projecting into the same
-- `promptVersions/{id}@{v}` would each overwrite the other on save, and the
-- last writer would win silently -- which is a worse version of the bug this
-- ticket is about.
create unique index if not exists prompts_engine_ref_key
    on config.prompts (engine_prompt_id, engine_version)
    where engine_prompt_id is not null;

create index if not exists prompts_engine_prompt_id_idx
    on config.prompts (engine_prompt_id)
    where engine_prompt_id is not null;

comment on column config.prompts.engine_version is
    'The version inside the pinned skillRef, as a STRING because the engine '
    'treats it as one. It does not increment when a prompt is edited -- '
    'prompt_versions.version does.';

comment on column config.prompts.projected_version is
    'Which version is currently in the engine''s document. Null means nothing '
    'has been projected yet; a value lower than max(prompt_versions.version) '
    'means a save did not finish, which is a state worth being able to see.';

-- --------------------------------------------------------------------
-- Where a version came from
-- --------------------------------------------------------------------
--
-- S7 imports the `supersededHistory` array the old endpoint kept -- capped at
-- ten entries, with no restore path -- into real versions. An imported version
-- and an authored one are not the same claim about history: the imported ones
-- have no reliable order beyond the array's, and everything before the
-- eleventh edit is simply gone. Recording which is which means nobody later
-- reads the import as a complete history.

alter table config.prompt_versions
    add column if not exists origin text not null default 'authored';

-- `alter table ... add constraint` has no IF NOT EXISTS, and this file is an
-- ALTER somebody will plausibly re-run. Guarded so the whole migration is
-- idempotent rather than idempotent in its column adds and not in its
-- constraints -- which is worse than being uniformly neither, because the
-- `if not exists` above implies a property the file would not have.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'config.prompts'::regclass
           and conname = 'prompts_engine_ref_is_whole'
    ) then
        alter table config.prompts
            add constraint prompts_engine_ref_is_whole
            check (
                (engine_prompt_id is null and engine_version is null)
             or (engine_prompt_id is not null and engine_version is not null)
            );
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'config.prompts'::regclass
           and conname = 'prompts_projection_is_whole'
    ) then
        alter table config.prompts
            add constraint prompts_projection_is_whole
            check (
                (projected_version is null and projected_at is null)
             or (projected_version is not null and projected_at is not null)
            );
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'config.prompts'::regclass
           and conname = 'prompts_projection_needs_a_target'
    ) then
        alter table config.prompts
            add constraint prompts_projection_needs_a_target
            check (projected_version is null or engine_prompt_id is not null);
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'config.prompt_versions'::regclass
           and conname = 'prompt_versions_origin_vocabulary'
    ) then
        alter table config.prompt_versions
            add constraint prompt_versions_origin_vocabulary
            check (origin in ('authored', 'imported', 'restored'));
    end if;
end $$;

comment on column config.prompt_versions.origin is
    'authored -- written through the API. imported -- recovered from the '
    'engine document or from supersededHistory, which was capped at ten '
    'entries, so an import is a floor on the history and not the whole of it. '
    'restored -- a copy of an earlier version, with restored_from_version_id '
    'naming it.';

insert into config.schema_migrations (filename) values ('0004_prompt_projection.sql')
on conflict (filename) do nothing;

commit;
