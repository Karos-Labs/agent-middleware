-- 0005 — where an agent's stage list comes from, and what follows from that.
--
-- S5 (SCRUM-220) set out to import from five registries and make Postgres the
-- editing surface. Four of the five import cleanly. The fifth does not, and
-- the reason is worth a column rather than a paragraph in a report nobody
-- re-reads.
--
-- A step in this schema must satisfy its kind (0001's
-- `agent_version_steps_20_kind_guard`): an `ai` step needs a prompt version
-- and a non-empty output schema, a `code` step needs code and a language. That
-- is right, and it is the whole value of the table.
--
-- The thirteen hand-written agent-engine workflows cannot satisfy it. Their
-- stage list is COMPILED TYPESCRIPT, mirrored into Firestore by
-- `generate_engine_stages.py` for display: of 291 recorded stages, 27 carry a
-- skillRef and none carries code, because the code is a program. Importing
-- them as steps would mean inventing a prompt, an output schema or a script
-- for roughly 260 stages — and a version assembled that way is not a record
-- of what runs, it is a plausible-looking guess that the ExecutionSnapshot
-- resolver (S6) would then freeze and hand to the engine as fact.
--
-- So the column says which it is, and two constraints make the distinction
-- load-bearing rather than advisory.
--
--   config       the stage list is data in this schema and may be published.
--                Dynamic agents, and anything authored in the Studio.
--   engine_code  the stage list is a program. Prompts, per-stage models and
--                tool grants are still configuration and still live here; the
--                LIST is not ours to publish.
--
-- This is also what makes S5 reversible in the sense its ticket asks for:
-- nothing here replaces a Firestore document, so dropping this schema leaves
-- the documents as the source of truth exactly as they are today.

begin;

alter table config.agents
    add column if not exists stage_source text not null default 'config';

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'agents_stage_source_vocabulary'
    ) then
        alter table config.agents
            add constraint agents_stage_source_vocabulary
            check (stage_source in ('config', 'engine_code'));
    end if;
end;
$$;

-- An agent whose stages are code cannot have a live version.
--
-- A trigger and not a CHECK, because the rule spans two columns whose writes
-- arrive from different directions: `published_version_id` is moved by a
-- publish or a rollback, `stage_source` by an import. Either one can be the
-- write that would break the pair.
--
-- Without this the failure is quiet and late: an import marks an agent
-- `engine_code`, someone publishes a version anyway, the resolver freezes it,
-- and the engine receives a step list that does not match the program it is
-- about to run. Refused here, the same mistake is an error on the write that
-- makes it.
create or replace function config.guard_stage_source_pointer() returns trigger
language plpgsql as $$
begin
    if new.stage_source = 'engine_code' and new.published_version_id is not null then
        raise exception
            'agent %: stage_source is "engine_code", so its stage list is a '
            'compiled workflow and cannot have a published version. Prompts, '
            'per-stage models and tool grants for it are still configuration; '
            'the step list is not.', new.slug
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists agents_20_stage_source_guard on config.agents;
create trigger agents_20_stage_source_guard
    before insert or update on config.agents
    for each row execute function config.guard_stage_source_pointer();

comment on column config.agents.stage_source is
    'config = the stage list is data here and may be published. '
    'engine_code = the stage list is a compiled workflow; only its prompts, '
    'models and tool grants are configuration.';

insert into config.schema_migrations (filename) values ('0005_stage_source.sql')
on conflict (filename) do nothing;

commit;
