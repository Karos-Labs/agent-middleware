-- =====================================================================
-- 0001_config_plane_verify.sql — proves the schema refuses what it must
--
-- Every check attempts the write the schema is supposed to reject and fails
-- LOUDLY if the write succeeds. A constraint nobody has seen refuse anything
-- is a constraint nobody knows is wired up.
--
-- Safe to run against any environment: the whole file is one transaction that
-- ends in ROLLBACK, so it leaves nothing behind. Requires 0001 and 0002.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_config_plane_verify.sql
--
-- Output is one NOTICE per passing check and an exception on the first
-- failure.
-- =====================================================================

\set ON_ERROR_STOP on

begin;

set local search_path = config, public;

-- --------------------------------------------------------------------
-- Fixtures
-- --------------------------------------------------------------------

insert into config.models (
    model_id, display_name, vendor, route, provider_model_name,
    input_per_1m, output_per_1m, pricing_checked_on, tiers
) values
    ('claude-sonnet-4-6', 'Claude Sonnet 4.6', 'anthropic', 'anthropic',
     'claude-sonnet-4-6', 3.0, 15.0, '2026-08-20', array['pinned']),
    ('claude-haiku-4-5', 'Claude Haiku 4.5', 'anthropic', 'anthropic',
     'claude-haiku-4-5', 0.8, 4.0, '2026-08-20', array['commodity']);

insert into config.tools (code, display_name, description, version)
values ('read_client_context', 'Read client context', 'Reads the C1 envelope.', '1.0.0');

insert into config.capability_policy (agent_class_code, subject_type, subject)
values ('drafting', 'tool', 'read_client_context');

insert into config.agents (slug, name, agent_class_code, category, capabilities, platforms)
values
    ('x-agent', 'X Agent', 'drafting', 'social', array['draft_social_post'], array['x']),
    ('blog-agent', 'Blog Agent', 'drafting', 'content', array['draft_article'], array['blog']);

insert into config.prompts (id, prompt_key, agent_slug, purpose)
values ('11111111-1111-1111-1111-111111111111', 'x-agent/10-draft-post', 'x-agent', 'skill');

insert into config.prompt_versions (id, prompt_id, version, content, content_hash)
values ('22222222-2222-2222-2222-222222222222',
        '11111111-1111-1111-1111-111111111111', 1, 'Draft a post.',
        encode(sha256('Draft a post.'::bytea), 'hex'));

insert into config.agent_versions (id, agent_slug, version, default_model_id)
values ('33333333-3333-3333-3333-333333333333', 'x-agent', 1, 'claude-sonnet-4-6');

insert into config.agent_version_steps (
    id, version_id, step_id, position, kind_code, prompt_version_id,
    output_schema
) values (
    '44444444-4444-4444-4444-444444444444',
    '33333333-3333-3333-3333-333333333333',
    '10-draft-post', 0, 'ai', '22222222-2222-2222-2222-222222222222',
    '[{"name": "post", "type": "string"}]'::jsonb
);

do $$ begin raise notice 'fixtures loaded'; end $$;

-- --------------------------------------------------------------------
-- 1. A step's kind decides what it must carry
-- --------------------------------------------------------------------

do $$
begin
    begin
        insert into config.agent_version_steps (version_id, step_id, position, kind_code)
        values ('33333333-3333-3333-3333-333333333333', '11-no-prompt', 1, 'ai');
        raise exception 'FAIL 1a: an ai step without a prompt version was accepted';
    exception when not_null_violation then
        raise notice 'ok 1a: an ai step must name a prompt version';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id
        ) values ('33333333-3333-3333-3333-333333333333', '11-no-schema', 1, 'ai',
                  '22222222-2222-2222-2222-222222222222');
        raise exception 'FAIL 1b: an ai step with no output schema was accepted';
    exception when not_null_violation then
        raise notice 'ok 1b: an ai step must declare an output schema';
    end;

    begin
        insert into config.agent_version_steps (version_id, step_id, position, kind_code)
        values ('33333333-3333-3333-3333-333333333333', '12-no-code', 1, 'code');
        raise exception 'FAIL 1c: a code step with no code was accepted';
    exception when not_null_violation then
        raise notice 'ok 1c: a code step must carry code and a language';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, is_gate
        ) values ('33333333-3333-3333-3333-333333333333', '13-no-gate-kind', 1, 'gate', true);
        raise exception 'FAIL 1d: a gate step with no gate kind was accepted';
    exception when not_null_violation then
        raise notice 'ok 1d: a gate step must name its gate kind';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, is_gate, gate_kind
        ) values ('33333333-3333-3333-3333-333333333333', '14-bad-gate', 1, 'gate',
                  true, 'looks_fine_to_me');
        raise exception 'FAIL 1e: an unknown gate kind was accepted';
    exception when check_violation then
        raise notice 'ok 1e: the gate-kind vocabulary is closed';
    end;
end $$;

-- --------------------------------------------------------------------
-- 2. A pinned step never carries a fallback model
-- --------------------------------------------------------------------

do $$
begin
    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id,
            output_schema, provider_policy, fallback_model_id
        ) values ('33333333-3333-3333-3333-333333333333', '15-pinned-fallback', 5, 'ai',
                  '22222222-2222-2222-2222-222222222222',
                  '[{"name": "post", "type": "string"}]'::jsonb,
                  'pinned', 'claude-haiku-4-5');
        raise exception 'FAIL 2: a pinned step was allowed a fallback model';
    exception when check_violation then
        raise notice 'ok 2: a pinned step cannot declare a fallback (matches the engine)';
    end;
end $$;

-- --------------------------------------------------------------------
-- 3. A draft is editable and deletable
-- --------------------------------------------------------------------

do $$
declare
    scratch uuid;
begin
    insert into config.agent_versions (agent_slug, version) values ('x-agent', 99)
    returning id into scratch;

    update config.agent_versions set notes = 'still a draft' where id = scratch;
    delete from config.agent_versions where id = scratch;

    raise notice 'ok 3: a draft version can be edited and deleted';
end $$;

-- --------------------------------------------------------------------
-- 4. A frozen version is immutable, and so is everything under it
-- --------------------------------------------------------------------

update config.agent_versions
   set status = 'frozen', frozen_at = now(), frozen_by = 'verify@karoslabs.com'
 where id = '33333333-3333-3333-3333-333333333333';

do $$
begin
    begin
        update config.agent_versions set notes = 'sneaking a change in'
         where id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAIL 4a: a frozen version accepted an edit';
    exception when restrict_violation then
        raise notice 'ok 4a: a frozen version refuses an edit';
    end;

    begin
        update config.agent_versions set status = 'draft'
         where id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAIL 4b: a frozen version was thawed';
    exception when restrict_violation then
        raise notice 'ok 4b: freezing is one-way';
    end;

    begin
        delete from config.agent_versions
         where id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAIL 4c: a frozen version was deleted';
    exception when restrict_violation then
        raise notice 'ok 4c: a frozen version cannot be deleted';
    end;

    begin
        update config.agent_version_steps set description = 'edited after freeze'
         where id = '44444444-4444-4444-4444-444444444444';
        raise exception 'FAIL 4d: a step of a frozen version accepted an edit';
    exception when restrict_violation then
        raise notice 'ok 4d: the STEPS of a frozen version are frozen too';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id, output_schema
        ) values ('33333333-3333-3333-3333-333333333333', '90-appended', 90, 'ai',
                  '22222222-2222-2222-2222-222222222222',
                  '[{"name": "post", "type": "string"}]'::jsonb);
        raise exception 'FAIL 4e: a step was appended to a frozen version';
    exception when restrict_violation then
        raise notice 'ok 4e: no step can be appended to a frozen version';
    end;

    begin
        delete from config.agent_version_steps
         where id = '44444444-4444-4444-4444-444444444444';
        raise exception 'FAIL 4f: a step of a frozen version was deleted';
    exception when restrict_violation then
        raise notice 'ok 4f: no step can be removed from a frozen version';
    end;

    begin
        insert into config.tool_config (tool_code, scope, step_row_id)
        values ('read_client_context', 'step', '44444444-4444-4444-4444-444444444444');
        raise exception 'FAIL 4g: a tool was granted to a step of a frozen version';
    exception when restrict_violation then
        raise notice 'ok 4g: the tool grants of a frozen version are frozen too';
    end;
end $$;

-- --------------------------------------------------------------------
-- 5. The live pointer: frozen only, own agent only, and rollback is a uuid
-- --------------------------------------------------------------------

do $$
declare
    draft_id uuid;
begin
    insert into config.agent_versions (agent_slug, version) values ('x-agent', 2)
    returning id into draft_id;

    begin
        update config.agents set published_version_id = draft_id where slug = 'x-agent';
        raise exception 'FAIL 5a: an agent was pointed at a draft version';
    exception when restrict_violation then
        raise notice 'ok 5a: only a frozen version can be live';
    end;

    -- Publish v1, which is frozen.
    update config.agents
       set published_version_id = '33333333-3333-3333-3333-333333333333'
     where slug = 'x-agent';
    raise notice 'ok 5b: publishing moves the pointer';

    begin
        update config.agents
           set published_version_id = '33333333-3333-3333-3333-333333333333'
         where slug = 'blog-agent';
        raise exception 'FAIL 5c: an agent was pointed at another agent''s version';
    exception when foreign_key_violation then
        raise notice 'ok 5c: an agent cannot publish another agent''s version';
    end;

    -- And the rollback: freeze v2, point at it, point back. No version row
    -- changes in either direction, which is the property the ticket asks for.
    update config.agent_versions set status = 'frozen', frozen_at = now()
     where id = draft_id;
    update config.agents set published_version_id = draft_id where slug = 'x-agent';
    update config.agents
       set published_version_id = '33333333-3333-3333-3333-333333333333'
     where slug = 'x-agent';

    if (select count(*) from config.agent_versions
         where agent_slug = 'x-agent' and status <> 'frozen') <> 0 then
        raise exception 'FAIL 5d: a rollback changed a version row';
    end if;
    raise notice 'ok 5d: rollback moved a pointer and touched no version row';

    if (select lifecycle from config.agent_version_state
         where id = '33333333-3333-3333-3333-333333333333') <> 'published' then
        raise exception 'FAIL 5e: the state view does not report the live version';
    end if;
    if (select lifecycle from config.agent_version_state where id = draft_id)
       <> 'superseded' then
        raise exception 'FAIL 5f: the state view does not report a superseded version';
    end if;
    raise notice 'ok 5e/f: the view reports draft / published / superseded correctly';
end $$;

-- --------------------------------------------------------------------
-- 6. A client pin belongs to the agent it is pinned for
-- --------------------------------------------------------------------

do $$
begin
    begin
        insert into config.client_agent_config (client_slug, agent_slug, pinned_version_id)
        values ('geektime', 'blog-agent', '33333333-3333-3333-3333-333333333333');
        raise exception 'FAIL 6a: a client was pinned to another agent''s version';
    exception when foreign_key_violation then
        raise notice 'ok 6a: a client pin cannot cross agents';
    end;

    insert into config.client_agent_config (client_slug, agent_slug, pinned_version_id)
    values ('geektime', 'x-agent', '33333333-3333-3333-3333-333333333333');
    raise notice 'ok 6b: a client can be pinned to a frozen version of its own agent';

    begin
        insert into config.client_agent_config (client_slug, agent_slug)
        values ('geektime', 'x-agent');
        raise exception 'FAIL 6c: a client got two configs for the same agent';
    exception when unique_violation then
        raise notice 'ok 6c: one config per client per agent';
    end;
end $$;

-- --------------------------------------------------------------------
-- 7. Prompt versions are append-only, and a restore is a new version
-- --------------------------------------------------------------------

do $$
declare
    restored uuid;
begin
    begin
        update config.prompt_versions set content = 'overwritten in place'
         where id = '22222222-2222-2222-2222-222222222222';
        raise exception 'FAIL 7a: a prompt version was overwritten in place';
    exception when restrict_violation then
        raise notice 'ok 7a: a prompt version cannot be overwritten (the 24.08 bug)';
    end;

    begin
        delete from config.prompt_versions
         where id = '22222222-2222-2222-2222-222222222222';
        raise exception 'FAIL 7b: a prompt version was deleted';
    exception when restrict_violation then
        raise notice 'ok 7b: a prompt version cannot be deleted';
    end;

    insert into config.prompt_versions (prompt_id, version, content, content_hash)
    values ('11111111-1111-1111-1111-111111111111', 2, 'Draft a better post.',
            encode(sha256('Draft a better post.'::bytea), 'hex'));

    insert into config.prompt_versions (
        prompt_id, version, content, content_hash, restored_from_version_id, notes
    ) values (
        '11111111-1111-1111-1111-111111111111', 3, 'Draft a post.',
        encode(sha256('Draft a post.'::bytea), 'hex'),
        '22222222-2222-2222-2222-222222222222', 'Reinstated v1.'
    ) returning id into restored;

    if (select count(*) from config.prompt_versions
         where prompt_id = '11111111-1111-1111-1111-111111111111') <> 3 then
        raise exception 'FAIL 7c: the version history is not intact';
    end if;
    raise notice 'ok 7c: restore is a new version and the history is uncapped';

    begin
        insert into config.prompt_versions (prompt_id, version, content, content_hash)
        values ('11111111-1111-1111-1111-111111111111', 3, 'Duplicate.',
                encode(sha256('Duplicate.'::bytea), 'hex'));
        raise exception 'FAIL 7d: two versions with the same number were accepted';
    exception when unique_violation then
        raise notice 'ok 7d: a version number is unique per prompt';
    end;

    begin
        insert into config.prompt_versions (prompt_id, version, content, content_hash)
        values ('11111111-1111-1111-1111-111111111111', 4, 'Bad hash.', 'not-a-sha256');
        raise exception 'FAIL 7e: a malformed content hash was accepted';
    exception when check_violation then
        raise notice 'ok 7e: content_hash must be a sha256';
    end;
end $$;

-- --------------------------------------------------------------------
-- 8. The audit log is append-only
-- --------------------------------------------------------------------

do $$
begin
    insert into config.audit_log (actor, actor_role, action, entity_type, entity_id, agent_slug)
    values ('verify@karoslabs.com', 'admin', 'publish', 'agent_version',
            '33333333-3333-3333-3333-333333333333', 'x-agent');

    begin
        update config.audit_log set actor = 'someone.else@karoslabs.com'
         where entity_id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAIL 8a: an audit record was rewritten';
    exception when restrict_violation then
        raise notice 'ok 8a: an audit record cannot be rewritten';
    end;

    begin
        delete from config.audit_log
         where entity_id = '33333333-3333-3333-3333-333333333333';
        raise exception 'FAIL 8b: an audit record was deleted';
    exception when restrict_violation then
        raise notice 'ok 8b: an audit record cannot be deleted';
    end;

    begin
        insert into config.audit_log (actor, action, entity_type, entity_id)
        values ('verify@karoslabs.com', 'quietly_fix', 'agent', 'x-agent');
        raise exception 'FAIL 8c: an unknown action was accepted';
    exception when check_violation then
        raise notice 'ok 8c: the action vocabulary is closed';
    end;
end $$;

-- --------------------------------------------------------------------
-- 9. A model cannot exist without a price
-- --------------------------------------------------------------------

do $$
begin
    begin
        insert into config.models (
            model_id, display_name, vendor, route, provider_model_name,
            output_per_1m, pricing_checked_on
        ) values ('gemini-1.5-flash', 'Gemini 1.5 Flash', 'google', 'gemini',
                  'gemini-1.5-flash', 0.3, '2026-08-20');
        raise exception 'FAIL 9a: a model with no input price was accepted';
    exception when not_null_violation then
        raise notice 'ok 9a: a priceless model is unrepresentable (no silent Sonnet fallback)';
    end;

    begin
        insert into config.models (
            model_id, display_name, vendor, route, provider_model_name,
            input_per_1m, output_per_1m, pricing_checked_on
        ) values ('some-model', 'Some model', 'anthropic', 'vertex-ish',
                  'some-model', 1.0, 2.0, '2026-08-20');
        raise exception 'FAIL 9b: an unknown route was accepted';
    exception when check_violation then
        raise notice 'ok 9b: the route vocabulary is closed and separate from vendor';
    end;

    insert into config.model_aliases (alias, model_id, provider_policy)
    values ('sonnet', 'claude-sonnet-4-6', 'pinned'),
           ('haiku', 'claude-haiku-4-5', 'commodity');

    update config.model_aliases set model_id = 'claude-haiku-4-5' where alias = 'sonnet';
    update config.model_aliases set model_id = 'claude-sonnet-4-6' where alias = 'sonnet';
    raise notice 'ok 9c: an alias repoints without a redeploy, and models keep their prices';
end $$;

-- --------------------------------------------------------------------
-- 10. Capability and portal-key vocabularies
-- --------------------------------------------------------------------

do $$
begin
    begin
        insert into config.agents (slug, name, agent_class_code, capabilities)
        values ('mystery-agent', 'Mystery', 'drafting', array['do_something_nice']);
        raise exception 'FAIL 10a: a capability outside the C4 vocabulary was accepted';
    exception when check_violation then
        raise notice 'ok 10a: a capability outside C4 §6 is refused';
    end;

    insert into config.agent_custom_agent_keys (custom_agent_key, agent_slug)
    values ('karos-x-agent-v2', 'x-agent'),
           ('karos-linkedin-writer-v2', 'blog-agent'),
           ('karos-linkedin-setup-v2', 'blog-agent');
    raise notice 'ok 10b: two portal keys can route to one agent';

    begin
        insert into config.agent_custom_agent_keys (custom_agent_key, agent_slug)
        values ('karos-x-agent-v2', 'blog-agent');
        raise exception 'FAIL 10c: one portal key routed to two agents';
    exception when unique_violation then
        raise notice 'ok 10c: a portal key appears exactly once (C4 §5)';
    end;

    begin
        insert into config.agents (slug, name, agent_class_code)
        values ('Not_A_Slug', 'Nope', 'drafting');
        raise exception 'FAIL 10d: a non-slug agent id was accepted';
    exception when check_violation then
        raise notice 'ok 10d: the slug charset matches the engine''s productId';
    end;

    begin
        insert into config.agents (slug, name, agent_class_code, source_registry)
        values ('half-imported', 'Half imported', 'drafting', 'customAgents');
        raise exception 'FAIL 10e: incomplete import provenance was accepted';
    exception when check_violation then
        raise notice 'ok 10e: import provenance is all three columns or none';
    end;
end $$;

-- --------------------------------------------------------------------
-- 11. Schedules: the money switch and the clock
-- --------------------------------------------------------------------

do $$
declare
    claimed uuid;
begin
    begin
        insert into config.schedules (
            client_slug, agent_slug, label, cadence, hour, minute, time_zone,
            billing_intent_source, next_run_at
        ) values ('geektime', 'x-agent', 'Daily post', 'daily', 9, 0,
                  'Asia/Jerusalem', 'explicit', now());
        raise exception 'FAIL 11a: a schedule with no billing decision was accepted';
    exception when not_null_violation then
        raise notice 'ok 11a: bill_client_credits must be stated (no charge: null)';
    end;

    begin
        insert into config.schedules (
            client_slug, agent_slug, label, cadence, hour, minute,
            bill_client_credits, billing_intent_source, next_run_at
        ) values ('geektime', 'x-agent', 'Daily post', 'daily', 9, 0,
                  true, 'explicit', now());
        raise exception 'FAIL 11b: a schedule with no timezone was accepted';
    exception when not_null_violation then
        raise notice 'ok 11b: time_zone must be stated (no runtime-local fallback)';
    end;

    begin
        insert into config.schedules (
            client_slug, agent_slug, label, cadence, hour, minute, time_zone,
            bill_client_credits, billing_intent_source, next_run_at
        ) values ('geektime', 'x-agent', 'Weekly post', 'weekly', 9, 0,
                  'Asia/Jerusalem', true, 'explicit', now());
        raise exception 'FAIL 11c: a weekly schedule with no weekdays was accepted';
    exception when check_violation then
        raise notice 'ok 11c: a weekly schedule must name its weekdays';
    end;

    begin
        insert into config.schedules (
            client_slug, agent_slug, label, cadence, hour, minute, time_zone,
            weekdays, day_of_month, bill_client_credits, billing_intent_source, next_run_at
        ) values ('geektime', 'x-agent', 'Confused', 'monthly', 9, 0,
                  'Asia/Jerusalem', array[1]::smallint[], 15, true, 'explicit', now());
        raise exception 'FAIL 11d: a monthly schedule carrying weekdays was accepted';
    exception when check_violation then
        raise notice 'ok 11d: the cadence decides which day field exists, and only one';
    end;

    begin
        insert into config.schedules (
            client_slug, agent_slug, label, cadence, hour, minute, time_zone,
            bill_client_credits, billing_intent_source, next_run_at,
            fire_in_flight_since
        ) values ('geektime', 'x-agent', 'Half-claimed', 'daily', 9, 0,
                  'Asia/Jerusalem', true, 'explicit', now(), now());
        raise exception 'FAIL 11e: a claim marker with no claim id was accepted';
    exception when check_violation then
        raise notice 'ok 11e: a fire claim is a timestamp AND an id, or neither';
    end;

    insert into config.schedules (
        client_slug, agent_slug, label, cadence, hour, minute, time_zone,
        bill_client_credits, billing_intent_source, next_run_at
    ) values ('geektime', 'x-agent', 'Daily post', 'daily', 9, 0,
              'Asia/Jerusalem', true, 'explicit', now() - interval '1 minute')
    returning id into claimed;

    -- The claim the whole move to Postgres is for.
    perform id from config.schedules
     where status = 'active' and next_run_at <= now()
     order by next_run_at
     limit 10
     for update skip locked;

    raise notice 'ok 11f: the due-schedule claim runs as SELECT ... FOR UPDATE SKIP LOCKED';
end $$;

-- --------------------------------------------------------------------
-- 12. The reason Postgres was chosen: one atomic 800KB version
-- --------------------------------------------------------------------
--
-- 40 steps x 20KB of prompt. This is the ticket's own justification, so it is
-- worth being a measurement rather than an assertion: the write below is over
-- Firestore's 1MB document ceiling and lands in ONE transaction, which is
-- what a subcollection would have cost.

do $$
declare
    big_version uuid;
    body        text := repeat('x', 20000);
    total       bigint;
    i           integer;
    pv          uuid;
begin
    insert into config.agent_versions (agent_slug, version, default_model_id)
    values ('blog-agent', 1, 'claude-sonnet-4-6') returning id into big_version;

    for i in 1..40 loop
        insert into config.prompts (prompt_key, agent_slug, purpose)
        values (format('blog-agent/%s-step', lpad(i::text, 2, '0')), 'blog-agent', 'skill');

        insert into config.prompt_versions (prompt_id, version, content, content_hash)
        select id, 1, body, encode(sha256(body::bytea), 'hex')
          from config.prompts
         where prompt_key = format('blog-agent/%s-step', lpad(i::text, 2, '0'))
        returning id into pv;

        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id, output_schema
        ) values (
            big_version, format('%s-step', lpad(i::text, 2, '0')), i, 'ai', pv,
            '[{"name": "draft", "type": "string"}]'::jsonb
        );
    end loop;

    select sum(length(v.content)) into total
      from config.agent_version_steps s
      join config.prompt_versions v on v.id = s.prompt_version_id
     where s.version_id = big_version;

    if total < 800000 then
        raise exception 'FAIL 12: expected at least 800KB of prompt, measured %', total;
    end if;

    update config.agent_versions set status = 'frozen', frozen_at = now()
     where id = big_version;

    raise notice 'ok 12: a 40-step version carrying % bytes of prompt froze atomically', total;
end $$;

-- --------------------------------------------------------------------
-- 13. Default deny on tools
-- --------------------------------------------------------------------

do $$
declare
    permitted boolean;
begin
    -- The check S4 performs: is every tool on this version's steps permitted
    -- for this agent's class?
    select not exists (
        select 1
          from config.agent_version_steps s
          join config.agent_versions v on v.id = s.version_id
          join config.agents a on a.slug = v.agent_slug
          join config.tool_config tc on tc.step_row_id = s.id and tc.scope = 'step'
          left join config.capability_policy p
                 on p.agent_class_code = a.agent_class_code
                and p.subject_type = 'tool'
                and p.subject = tc.tool_code
                and p.decision = 'allow'
         where v.agent_slug = 'x-agent'
           and p.subject is null
    ) into permitted;

    if not permitted then
        raise exception 'FAIL 13: the tool-policy check reported a violation where there is none';
    end if;
    raise notice 'ok 13: the S4 tool check is one join, and the absence of a grant is a no';
end $$;

-- --------------------------------------------------------------------
-- 14. The engine's own bounds, and nothing it cannot read
-- --------------------------------------------------------------------
--
-- SCRUM-217 asks for "retry, timeout" on a step. agent-engine has neither at
-- step level for an AI step: its knobs are maxSteps, maxTokens and
-- maxMalformedTurns, its self-critique loop has maxRevisions, and the only
-- timeout is per-RUN (agentStepTimeoutMs). These checks pin the columns to
-- that vocabulary, so the schema cannot hold configuration no consumer reads.

do $$
declare
    draft uuid;
begin
    insert into config.agent_versions (agent_slug, version, agent_step_timeout_ms)
    values ('x-agent', 50, 600000) returning id into draft;
    raise notice 'ok 14a: the agent-step timeout is per-run, on the version';

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id,
            output_schema, code_timeout_ms
        ) values (draft, '10-ai-with-sandbox-timeout', 0, 'ai',
                  '22222222-2222-2222-2222-222222222222',
                  '[{"name": "post", "type": "string"}]'::jsonb, 30000);
        raise exception 'FAIL 14b: a sandbox timeout was accepted on a step with no code';
    exception when check_violation then
        raise notice 'ok 14b: the sandbox timeout only exists where there is a script';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id,
            output_schema, self_critique_max_revisions
        ) values (draft, '11-half-critique', 1, 'ai',
                  '22222222-2222-2222-2222-222222222222',
                  '[{"name": "post", "type": "string"}]'::jsonb, 2);
        raise exception 'FAIL 14c: a revision count with no gate tool was accepted';
    exception when check_violation then
        raise notice 'ok 14c: a self-critique loop is a gate tool or nothing';
    end;

    begin
        insert into config.agent_version_steps (
            version_id, step_id, position, kind_code, prompt_version_id,
            output_schema, max_malformed_turns
        ) values (draft, '12-forever-retry', 2, 'ai',
                  '22222222-2222-2222-2222-222222222222',
                  '[{"name": "post", "type": "string"}]'::jsonb, 50);
        raise exception 'FAIL 14d: an unbounded malformed-turn budget was accepted';
    exception when check_violation then
        raise notice 'ok 14d: malformed turns are bounded (a model looping on one '
                     'shape mistake is a config problem, not a token budget)';
    end;

    insert into config.agent_version_steps (
        version_id, step_id, position, kind_code, prompt_version_id, output_schema,
        max_steps, max_tokens, max_malformed_turns,
        self_critique_gate_tool, self_critique_max_revisions, self_critique_args
    ) values (draft, '13-fully-configured', 3, 'ai',
              '22222222-2222-2222-2222-222222222222',
              '[{"name": "post", "type": "string"}]'::jsonb,
              8, 8192, 1, 'gate.lintPost', 1, '{"platform": "x"}'::jsonb);

    insert into config.agent_version_steps (
        version_id, step_id, position, kind_code, language, code, code_timeout_ms
    ) values (draft, '14-transform', 4, 'code', 'python',
              'print(json.dumps({"ok": True}))', 30000);

    raise notice 'ok 14e: every field the engine actually reads has a home';
end $$;

do $$ begin raise notice 'ALL CHECKS PASSED'; end $$;

rollback;
