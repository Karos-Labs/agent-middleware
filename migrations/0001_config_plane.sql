-- =====================================================================
-- 0001_config_plane.sql — the configuration plane (SCRUM-217 / S2)
--
-- Everything *configurable* about the agent platform, in one place that can
-- hold it: 40 steps x 20KB of prompt is 800KB against Firestore's 1MB
-- document ceiling, and splitting a version into a subcollection loses the
-- atomic write that is the only thing making a version a version.
--
-- Two rules are enforced here rather than in the API, because a rule that
-- lives in application code survives exactly as long as the next refactor:
--
--   1. A frozen version is IMMUTABLE. Not by convention -- by trigger. The
--      guard compares the whole row minus the two columns a supersede is
--      allowed to touch, so a column added by a later migration is frozen
--      by default rather than accidentally editable.
--   2. The audit log is APPEND-ONLY. An audit trail that can be updated is
--      not an audit trail.
--
-- Conventions, following the repo's existing ones:
--   * Closed vocabularies that are pure lifecycle (status, availability) are
--     `text` + a named CHECK, not a native enum -- app/core/enums.py states
--     the reason: no enum type to ALTER when a value is added.
--   * Vocabularies that are REGISTRIES (step kinds, tools, models, agent
--     classes) are tables, so a reference is a foreign key and "does this
--     exist" is a lookup instead of a guess.
--   * `timestamptz` throughout. Nothing here stores epoch millis; the
--     karosCMO rows that do are converted on import (S5).
--
-- Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_config_plane.sql
-- Then prove it:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_config_plane_verify.sql
-- =====================================================================

begin;

create schema if not exists config;

set local search_path = config, public;

-- --------------------------------------------------------------------
-- Migration ledger
-- --------------------------------------------------------------------

create table if not exists config.schema_migrations (
    filename    text        primary key,
    applied_at  timestamptz not null default now(),
    applied_by  text        not null default current_user
);

comment on table config.schema_migrations is
    'One row per applied migration file. Checked by the deploy step so a '
    'migration cannot be applied twice or skipped.';

-- --------------------------------------------------------------------
-- Shared helpers
-- --------------------------------------------------------------------

create or replace function config.touch_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

comment on function config.touch_updated_at() is
    'BEFORE UPDATE trigger: keeps updated_at honest regardless of what the '
    'caller sent. Deliberately excluded from every immutability comparison '
    'below, so touching it can never be mistaken for editing a frozen row.';

-- The identifier charset every id in the three repos already uses:
-- agent-engine''s productId, AgentDefinition.agentId and StageIdSchema all
-- agree on lowercase-and-hyphens, so the database agrees with them.
create domain config.slug as text
    check (value ~ '^[a-z0-9][a-z0-9-]*$' and length(value) between 1 and 128);

comment on domain config.slug is
    'Lowercase-and-hyphens identifier. Same charset as agent-engine''s '
    'productId / AgentDefinition.agentId / StageIdSchema, so a slug is the '
    'same string in all three repositories.';

-- A code is a slug that also permits underscores: the capability and policy
-- vocabularies are snake_case (C4 §6: draft_social_post, run_seo_audit).
create domain config.code as text
    check (value ~ '^[a-z0-9][a-z0-9_.-]*$' and length(value) between 1 and 128);

-- --------------------------------------------------------------------
-- agent_classes — the policy grouping a capability check is made against
-- --------------------------------------------------------------------
--
-- Not the same axis as the catalog''s `category` (social / web / video /
-- research / content / reputation / seo), which is a display grouping in the
-- portal. A class answers a different question: what is this KIND of agent
-- permitted to do? S4''s publish validation resolves every tool against it.

create table config.agent_classes (
    code          config.code primary key,
    display_name  text        not null,
    description    text       not null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create trigger agent_classes_90_touch before update on config.agent_classes
    for each row execute function config.touch_updated_at();

comment on table config.agent_classes is
    'Policy grouping for agents. Distinct from the portal-facing `category`: '
    'a class decides what an agent MAY do (see capability_policy), a category '
    'decides where it appears in a list.';

-- --------------------------------------------------------------------
-- step_kinds — the step vocabulary, and the schema for each kind
-- --------------------------------------------------------------------
--
-- The engine has two spellings for the same set. `engine_stages.json` records
-- code / agent / gate (250 stages across 15 products); the dynamic-agent
-- definition schema calls the model-driven one `ai` and defaults a stage with
-- no kind to it. `engine_alias` records that so the import (S5) does not have
-- to hard-code the mapping, and so nobody has to remember which repo says
-- which word.
--
-- `config_schema` is where S4''s "schemas for every step" lives, and the four
-- `requires_*` flags are what the per-step guard below enforces -- adding a
-- kind is then a row, not a new CHECK constraint.

create table config.step_kinds (
    code                    config.code primary key,
    engine_alias            config.code,
    display_name            text        not null,
    description             text        not null,
    config_schema           jsonb       not null default '{}'::jsonb,
    requires_prompt         boolean     not null default false,
    requires_code           boolean     not null default false,
    requires_output_schema  boolean     not null default false,
    requires_gate_kind      boolean     not null default false,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),

    constraint step_kinds_alias_differs
        check (engine_alias is null or engine_alias <> code)
);

create unique index step_kinds_engine_alias_key
    on config.step_kinds (engine_alias) where engine_alias is not null;

create trigger step_kinds_90_touch before update on config.step_kinds
    for each row execute function config.touch_updated_at();

comment on column config.step_kinds.engine_alias is
    'The other spelling the engine uses for this kind (engine_stages.json '
    'says "agent" where the definition schema says "ai"). Unique, so two '
    'kinds cannot claim the same alias.';

-- --------------------------------------------------------------------
-- tools — the registry a policy and a step refer to
-- --------------------------------------------------------------------
--
-- The engine''s live AgentToolRegistry stays the authority on whether a tool
-- can actually be CALLED. This table is what a policy and a step REFERENCE,
-- which is what turns "is this tool permitted for this agent class" into a
-- join instead of a hard-coded list. `version` is here because it travels
-- into every telemetry record (RFC-01 §9.1 rule 5).

create table config.tools (
    code          config.code primary key,
    display_name  text        not null,
    description   text        not null,
    version       text        not null,
    input_schema  jsonb       not null default '{}'::jsonb,
    status        text        not null default 'available',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint tools_status_vocabulary
        check (status in ('available', 'deprecated', 'retired'))
);

create trigger tools_90_touch before update on config.tools
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- models — the catalog, with pricing that cannot be absent
-- --------------------------------------------------------------------
--
-- Mirrors app/api/schemas/model.py column for column, plus the two things
-- S12 (SCRUM-222) needs and the Firestore collection does not have.
--
-- FIRST: pricing is NOT NULL on the model row rather than a separate
-- optional table. `pricingForModel` in the engine falls back to
-- DEFAULT_MODEL_PRICING ($3/$15, Sonnet''s) on a miss, silently, which bills
-- Opus work at a third of its cost and gemini-1.5-flash -- the tertiary
-- fallback, the one hop that does change model identity -- at ten times its
-- own. A plausible wrong number is the worst failure available here, so the
-- schema makes a priceless model unrepresentable: there is no row to miss.
--
-- SECOND: two vendor axes, deliberately separate columns, because the two
-- repositories use the SAME WORD for different questions.
--   * `vendor`  -- who makes the model (agent-middleware''s ModelVendor:
--                  anthropic / google / meta / other).
--   * `route`   -- how this deployment reaches it (agent-engine''s own
--                  ModelVendorSchema: anthropic / gemini / model-garden /
--                  openai-compatible).
-- Claude on Agent Platform is vendor=anthropic, route=anthropic; Llama on
-- Model Garden is vendor=meta, route=model-garden. Collapsing them into one
-- column is how a Studio author ends up unable to express a model that
-- exists.

create table config.models (
    model_id              text        primary key,
    display_name          text        not null,
    vendor                text        not null,
    route                 text        not null,
    availability          text        not null default 'available',
    provider_model_name   text        not null,
    region                text,
    description           text,
    context_window        integer,
    supports_tools        boolean     not null default true,
    tiers                 text[]      not null default '{}',
    -- USD per 1M tokens. cached_input_per_1m is nullable: absent means "the
    -- CACHE_READ_DISCOUNT of 0.1 x input", which is what the engine already
    -- assumes for every model that supports prompt caching.
    input_per_1m          numeric(12, 6) not null,
    output_per_1m         numeric(12, 6) not null,
    cached_input_per_1m   numeric(12, 6),
    pricing_source        text        not null default 'vendor_price_list',
    pricing_checked_on    date        not null,
    notes                 text,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),

    constraint models_model_id_charset
        check (model_id ~ '^[a-z0-9][a-z0-9.\-@]*$' and length(model_id) <= 128),
    constraint models_vendor_vocabulary
        check (vendor in ('anthropic', 'google', 'meta', 'mistral', 'openai', 'other')),
    constraint models_route_vocabulary
        check (route in ('anthropic', 'gemini', 'model-garden', 'openai-compatible')),
    constraint models_availability_vocabulary
        check (availability in ('available', 'not_enabled', 'retired')),
    constraint models_tiers_vocabulary
        check (tiers <@ array['pinned', 'portable', 'commodity']::text[]),
    constraint models_prices_are_positive
        check (input_per_1m >= 0 and output_per_1m >= 0
               and (cached_input_per_1m is null or cached_input_per_1m >= 0)),
    constraint models_context_window_sane
        check (context_window is null or context_window > 0)
);

create trigger models_90_touch before update on config.models
    for each row execute function config.touch_updated_at();

comment on column config.models.pricing_checked_on is
    'The date the two prices above were read off the vendor''s price list. '
    'Required, because "we do not know how old this number is" is the state '
    'the engine''s hard-coded table is in today.';

-- --------------------------------------------------------------------
-- model_aliases — the Studio''s three-option picker, as data
-- --------------------------------------------------------------------
--
-- MODEL_ALIASES is three `as const` lines in packages/core/src/router/
-- aliases.ts, which means a new Sonnet generation is a code change and a
-- redeploy: precisely what an alias exists to prevent. A table instead.
--
-- Its own table rather than a nullable column on `models`, because an alias
-- is a POINTER: `sonnet` points at claude-sonnet-4-6 today and at something
-- else next quarter, while claude-sonnet-4-6 keeps its own row and its own
-- price for every run that already referenced it.

create table config.model_aliases (
    alias            config.code primary key,
    model_id         text        not null references config.models (model_id),
    provider_policy  text        not null,
    description      text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),

    constraint model_aliases_policy_vocabulary
        check (provider_policy in ('pinned', 'portable', 'commodity'))
);

create index model_aliases_model_id_idx on config.model_aliases (model_id);

create trigger model_aliases_90_touch before update on config.model_aliases
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- agents — identity. The slug is the identity (C4 §8 invariant 1).
-- --------------------------------------------------------------------

create table config.agents (
    slug                  config.slug primary key,
    name                  text        not null,
    description           text        not null default '',
    agent_class_code      config.code not null references config.agent_classes (code),
    category              text,
    tags                  text[]      not null default '{}',
    credit_cost           integer     not null default 0,
    is_public             boolean     not null default true,
    status                text        not null default 'enabled',

    -- C4 §7.2: written once in the catalog, not derivable from the workflow.
    capabilities          text[]      not null default '{}',
    platforms             text[]      not null default '{}',
    consumes_media        boolean     not null default false,
    supports_target_date  boolean     not null default false,

    -- The pointer. What runs right now, and the only thing a rollback moves.
    -- FK added after agent_versions exists (the two reference each other).
    published_version_id  uuid,

    -- S5 provenance: which of the five registries this row was imported from,
    -- and its id there. Non-null on imported rows, null on rows authored in
    -- Postgres. This is what makes the import one-way and reversible.
    source_registry       text,
    source_id             text,
    imported_at           timestamptz,

    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    created_by            text,
    updated_by            text,

    constraint agents_status_vocabulary
        check (status in ('enabled', 'disabled', 'legacy_only')),
    constraint agents_credit_cost_non_negative
        check (credit_cost >= 0),
    -- C4 §6: a closed vocabulary, extended only by PR -- which is what a
    -- CHECK constraint in a migration is.
    constraint agents_capabilities_vocabulary
        check (capabilities <@ array[
            'draft_social_post', 'draft_article', 'draft_newsletter', 'draft_reply',
            'build_landing_page', 'produce_video', 'produce_carousel',
            'run_seo_audit', 'run_intel_report', 'run_setup', 'orchestrate_campaign'
        ]::text[]),
    constraint agents_source_registry_vocabulary
        check (source_registry is null or source_registry in (
            'customAgents', 'dynamicAgentSpecs', 'agentDefinitions',
            'middleware_agents', 'prompts'
        )),
    constraint agents_provenance_is_complete
        check ((source_registry is null and source_id is null and imported_at is null)
               or (source_registry is not null and source_id is not null
                   and imported_at is not null))
);

create index agents_class_idx on config.agents (agent_class_code);
create index agents_status_idx on config.agents (status);

create trigger agents_90_touch before update on config.agents
    for each row execute function config.touch_updated_at();

comment on column config.agents.published_version_id is
    'THE pointer to what runs now. Publish moves it forward, rollback moves '
    'it back, and neither touches a version row -- which is what makes '
    '"rollback is moving a pointer, no data change, no deletion" literally '
    'true. The history of where it pointed and when lives in audit_log.';

-- --------------------------------------------------------------------
-- agent_custom_agent_keys — the portal keys that route to an agent
-- --------------------------------------------------------------------
--
-- C4 §5 spent a section establishing that this is a LIST (fourteen portal
-- keys route to twelve engine products: karos-linkedin-writer-v2 and
-- karos-linkedin-setup-v2 both mean linkedin-agent). It also states the
-- constraint that matters: the union across all agents contains each portal
-- key EXACTLY ONCE.
--
-- As a text[] column that constraint is a test someone has to remember to
-- write. As a table it is the primary key.

create table config.agent_custom_agent_keys (
    custom_agent_key  text        primary key,
    agent_slug        config.slug not null
                      references config.agents (slug) on delete cascade,
    created_at        timestamptz not null default now()
);

create index agent_custom_agent_keys_agent_idx
    on config.agent_custom_agent_keys (agent_slug);

-- --------------------------------------------------------------------
-- capability_policy — what a class may do. Default deny.
-- --------------------------------------------------------------------
--
-- S4''s publish validation asks "is every tool in this version permitted for
-- this agent''s class". The absence of a row is a NO. That direction is the
-- whole value: a tool nobody has thought about is not silently available to
-- an agent that should not have it, and adding a capability is a visible act.

create table config.capability_policy (
    agent_class_code  config.code not null
                      references config.agent_classes (code) on delete cascade,
    subject_type      text        not null,
    subject           config.code not null,
    decision          text        not null default 'allow',
    note              text,
    created_at        timestamptz not null default now(),

    primary key (agent_class_code, subject_type, subject),

    constraint capability_policy_subject_type_vocabulary
        check (subject_type in ('tool', 'capability', 'model_tier')),
    constraint capability_policy_decision_vocabulary
        check (decision in ('allow', 'deny'))
);

comment on table config.capability_policy is
    'Per-agent-class grants. DEFAULT DENY: no row means not permitted. An '
    'explicit deny row exists so an inherited or blanket allow can be '
    'overridden visibly rather than by deleting a grant.';

-- --------------------------------------------------------------------
-- prompts / prompt_versions — one store, immutable versions
-- --------------------------------------------------------------------
--
-- S7 (SCRUM-221) counts six places a prompt lives today. The worst is the one
-- Studio has been writing to since 24.08: PUT /engine-prompts/{id}/versions/
-- {v} OVERWRITES the content in place and keeps the displaced text in a
-- `supersededHistory` array CAPPED AT TEN ENTRIES on the parent document,
-- with no restore endpoint. Three separate failures: the edit is destructive,
-- the history silently forgets its eleventh entry, and what it does remember
-- cannot be recovered through the API.
--
-- Here: a version row is append-only (no UPDATE, no DELETE -- see the guard
-- below), the history is the version list and has no cap, and a restore is a
-- NEW version recording where it came from. Nothing is ever displaced.

create table config.prompts (
    id                 uuid        primary key default gen_random_uuid(),
    prompt_key         text        not null unique,
    agent_slug         config.slug references config.agents (slug) on delete restrict,
    purpose            text        not null default 'system',
    description        text,
    -- Which version resolves when a caller does not pin one. Pinning by
    -- exact version happens on the step (agent_version_steps.prompt_version_id),
    -- which is what makes a frozen agent version immune to a prompt edit.
    active_version_id  uuid,
    source_registry    text,
    source_id          text,
    imported_at        timestamptz,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    created_by         text,

    constraint prompts_key_charset
        check (prompt_key ~ '^[a-z0-9][a-z0-9._/-]*$' and length(prompt_key) <= 256),
    constraint prompts_purpose_vocabulary
        check (purpose in ('system', 'skill', 'gate', 'template', 'other')),
    constraint prompts_source_registry_vocabulary
        check (source_registry is null or source_registry in (
            'engine_prompts', 'middleware_prompts', 'firestore_prompt_store',
            'vertex_prompt_store', 'file_prompt_store', 'dynamicAgentSpecs'
        ))
);

create index prompts_agent_idx on config.prompts (agent_slug);

create trigger prompts_90_touch before update on config.prompts
    for each row execute function config.touch_updated_at();

comment on column config.prompts.source_registry is
    'Which of S7''s six stores this prompt came from. The list is the six: '
    'engine-prompts (the destructive one), the middleware''s own prompts '
    'subcollection, and the engine''s Firestore / Vertex / file prompt '
    'stores, plus a spec''s inline systemPrompt.';

create table config.prompt_versions (
    id                        uuid        primary key default gen_random_uuid(),
    prompt_id                 uuid        not null
                              references config.prompts (id) on delete restrict,
    version                   integer     not null,
    content                   text        not null,
    -- sha256 of content. Cheap dedupe, and the stored answer to "is the
    -- prompt that produced this deliverable the one in the box today".
    content_hash              text        not null,
    variables                 text[]      not null default '{}',
    notes                     text,
    -- The restore path S7 asks for: a restore is a new version that records
    -- which one it reinstated. Never a mutation of the old row.
    restored_from_version_id  uuid        references config.prompt_versions (id),
    created_at                timestamptz not null default now(),
    created_by                text,

    constraint prompt_versions_version_positive check (version >= 1),
    constraint prompt_versions_content_not_empty check (length(content) > 0),
    constraint prompt_versions_hash_shape check (content_hash ~ '^[0-9a-f]{64}$'),
    unique (prompt_id, version)
);

create index prompt_versions_prompt_idx
    on config.prompt_versions (prompt_id, version desc);
create index prompt_versions_content_hash_idx
    on config.prompt_versions (content_hash);

alter table config.prompts
    add constraint prompts_active_version_fk
    foreign key (active_version_id) references config.prompt_versions (id);

-- --------------------------------------------------------------------
-- agent_versions — the unit that gets frozen
-- --------------------------------------------------------------------
--
-- Two statuses, not three. The ticket says "freeze the version, mark the
-- previous one superseded, move the pointer" and also, of rollback, "moving a
-- pointer. No data change, no deletion." Those two sentences cannot both hold
-- if `superseded` is a stored status: a rollback would have to un-supersede
-- the version it restores, and that is a data change.
--
-- So: a version is `draft` (editable) or `frozen` (forever). Which frozen
-- version is LIVE is agents.published_version_id and nothing else, so a
-- rollback writes one uuid and touches no version row. "Superseded" is then a
-- derived state -- frozen and not pointed at -- and the view
-- config.agent_version_state reports it for anyone who wants the three-state
-- vocabulary. When each version was live is in audit_log, with an actor and a
-- timestamp, which a status column never had.

create table config.agent_versions (
    id                       uuid        primary key default gen_random_uuid(),
    agent_slug               config.slug not null
                             references config.agents (slug) on delete restrict,
    version                  integer     not null,
    status                   text        not null default 'draft',
    default_model_id         text        references config.models (model_id),
    default_provider_policy  text        not null default 'pinned',
    dedupe_against_history   boolean     not null default false,
    -- Overrides DEFAULT_AGENT_STEP_TIMEOUT_MS (10 minutes) for every
    -- step.agent call in a run of this version. Per-run and not per-step,
    -- because that is the only shape WorkflowRuntime accepts.
    agent_step_timeout_ms    integer,
    notes                    text,
    frozen_at                timestamptz,
    frozen_by                text,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now(),
    created_by               text,

    constraint agent_versions_status_vocabulary
        check (status in ('draft', 'frozen')),
    constraint agent_versions_version_positive
        check (version >= 1),
    constraint agent_versions_provider_policy_vocabulary
        check (default_provider_policy in ('pinned', 'portable', 'commodity')),
    constraint agent_versions_step_timeout_positive
        check (agent_step_timeout_ms is null or agent_step_timeout_ms > 0),
    -- A frozen version records when and by whom; a draft records neither.
    constraint agent_versions_frozen_is_stamped
        check ((status = 'frozen' and frozen_at is not null)
               or (status = 'draft' and frozen_at is null and frozen_by is null)),
    unique (agent_slug, version),
    -- Lets the two pointers below be composite foreign keys, so "the version
    -- pinned for this client belongs to this agent" is declarative rather
    -- than a trigger nobody reads.
    unique (agent_slug, id)
);

create index agent_versions_agent_status_idx
    on config.agent_versions (agent_slug, status);

alter table config.agents
    add constraint agents_published_version_fk
    foreign key (slug, published_version_id)
    references config.agent_versions (agent_slug, id);

comment on constraint agents_published_version_fk on config.agents is
    'Composite on purpose: an agent cannot point at another agent''s version. '
    'A plain FK on published_version_id would happily let it.';

create or replace view config.agent_version_state as
select
    v.id,
    v.agent_slug,
    v.version,
    v.status,
    case
        when v.status = 'draft'                        then 'draft'
        when a.published_version_id = v.id             then 'published'
        else                                                'superseded'
    end as lifecycle,
    v.frozen_at,
    v.frozen_by,
    v.created_at
from config.agent_versions v
join config.agents a on a.slug = v.agent_slug;

comment on view config.agent_version_state is
    'The three-state vocabulary the ticket uses (draft / published / '
    'superseded), derived from the two-state stored status plus the pointer. '
    'Read this instead of adding a third status.';

-- --------------------------------------------------------------------
-- agent_version_steps — the graph
-- --------------------------------------------------------------------
--
-- One row per stage. `prompt_version_id` pins an EXACT prompt version rather
-- than "the active one", which is what makes a frozen agent version genuinely
-- frozen: editing the prompt afterwards publishes a new prompt version and
-- this row still points at the old one.

create table config.agent_version_steps (
    id                   uuid        primary key default gen_random_uuid(),
    version_id           uuid        not null
                         references config.agent_versions (id) on delete cascade,
    step_id              config.slug not null,
    position             integer     not null,
    kind_code            config.code not null references config.step_kinds (code),
    description          text        not null default '',

    -- AI / agent steps
    prompt_version_id    uuid        references config.prompt_versions (id),
    model_id             text        references config.models (model_id),
    provider_policy      text,
    fallback_model_id    text        references config.models (model_id),
    output_schema        jsonb,
    -- The engine's own bounds, named as the engine names them. SCRUM-217 asks
    -- for "retry, timeout"; agent-engine has neither at step level for an AI
    -- step, and inventing columns for them would mean storing configuration
    -- no consumer can read -- which is how a wrong implementation grows to
    -- match a schema. What it actually has:
    --   maxSteps            -- ReAct turn ceiling, default 8
    --   maxTokens           -- output-token ceiling for one turn; a turn that
    --                          runs out of room returns truncated, unparseable
    --                          structured output and the step fails outright,
    --                          so this is not a soft limit
    --   maxMalformedTurns   -- how many malformed turns are re-prompted with
    --                          the validation error before giving up; default 1
    max_steps            integer,
    max_tokens           integer,
    max_malformed_turns  integer,

    -- The self-critique gate, when a step has one. `gate_tool` is a tool name
    -- (e.g. "gate.brand_compliance"); S4 resolves it against `tools` at
    -- publish rather than a foreign key here, because a gate tool is not a
    -- tool the step may CALL -- it is one the step is measured by.
    self_critique_gate_tool      text,
    self_critique_max_revisions  integer,
    self_critique_args           jsonb,

    -- code steps
    language             text,
    code                 text,
    -- The sandbox's wall-clock budget for the script, and the ONLY per-step
    -- timeout the engine has. An AI step's timeout is per-RUN
    -- (WorkflowRuntime.agentStepTimeoutMs, default 10 minutes, applied to
    -- every step.agent call in the run), so it lives on agent_versions.
    code_timeout_ms      integer,

    -- gate steps
    is_gate              boolean     not null default false,
    gate_kind            config.code,

    skill_ref            text,
    config               jsonb       not null default '{}'::jsonb,

    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),

    unique (version_id, step_id),
    unique (version_id, position) deferrable initially immediate,

    constraint agent_version_steps_position_non_negative
        check (position >= 0),
    constraint agent_version_steps_provider_policy_vocabulary
        check (provider_policy is null
               or provider_policy in ('pinned', 'portable', 'commodity')),
    -- agent-engine rejects a fallback declared alongside `pinned` rather than
    -- ignoring it: a pinned step''s model is what it is, or the step fails
    -- loudly. The database says the same thing.
    constraint agent_version_steps_pinned_has_no_fallback
        check (fallback_model_id is null or coalesce(provider_policy, 'pinned') <> 'pinned'),
    constraint agent_version_steps_language_vocabulary
        check (language is null or language in ('node', 'python')),
    -- MAX_STAGE_CODE_CHARS, mirrored from the engine and from Studio''s own
    -- MAX_CODE_CHARS.
    constraint agent_version_steps_code_length
        check (code is null or length(code) <= 20000),
    constraint agent_version_steps_gate_kind_vocabulary
        check (gate_kind is null or gate_kind in (
            'batch_review', 'prompt_set_review', 'fix_generation_review'
        )),
    constraint agent_version_steps_gate_flag_agrees
        check ((is_gate and gate_kind is not null)
               or (not is_gate and gate_kind is null)),
    -- A timeout on a step the sandbox does not run is a number nothing reads.
    constraint agent_version_steps_code_timeout_is_for_code
        check (code_timeout_ms is null
               or (code_timeout_ms > 0 and code is not null)),
    constraint agent_version_steps_bounds_positive
        check ((max_steps is null or max_steps > 0)
               and (max_tokens is null or max_tokens > 0)
               and (max_malformed_turns is null or max_malformed_turns between 0 and 5)),
    -- A max_revisions or an args map with no gate tool configures nothing.
    constraint agent_version_steps_self_critique_is_whole
        check ((self_critique_gate_tool is not null
                and coalesce(self_critique_max_revisions, 1) > 0)
               or (self_critique_gate_tool is null
                   and self_critique_max_revisions is null
                   and self_critique_args is null))
);

create index agent_version_steps_version_position_idx
    on config.agent_version_steps (version_id, position);
create index agent_version_steps_prompt_version_idx
    on config.agent_version_steps (prompt_version_id);
create index agent_version_steps_model_idx
    on config.agent_version_steps (model_id);

create trigger agent_version_steps_90_touch before update on config.agent_version_steps
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- client_agent_config — per-tenant enablement and pinning
-- --------------------------------------------------------------------
--
-- `client_slug` is the same tenant key S8 (SCRUM-225) just put on agent_runs,
-- so "which configuration produced this deliverable, for whom" is one join
-- rather than two systems of record.

create table config.client_agent_config (
    id                   uuid        primary key default gen_random_uuid(),
    client_slug          text        not null,
    agent_slug           config.slug not null
                         references config.agents (slug) on delete cascade,
    enabled              boolean     not null default true,
    -- A version pinned for this client, overriding the agent pointer. C6''s
    -- resolver reads "the published version, or the one pinned to a client".
    pinned_version_id    uuid,
    credit_cost_override integer,
    overrides            jsonb       not null default '{}'::jsonb,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    updated_by           text,

    unique (client_slug, agent_slug),

    constraint client_agent_config_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint client_agent_config_credit_override_non_negative
        check (credit_cost_override is null or credit_cost_override >= 0),
    -- Same composite trick as the agent pointer: a client cannot be pinned to
    -- a version of a different agent.
    constraint client_agent_config_pinned_version_fk
        foreign key (agent_slug, pinned_version_id)
        references config.agent_versions (agent_slug, id)
);

create index client_agent_config_client_idx
    on config.client_agent_config (client_slug);
create index client_agent_config_pinned_idx
    on config.client_agent_config (pinned_version_id);

create trigger client_agent_config_90_touch before update on config.client_agent_config
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- tool_config — a tool grant, with its configuration, at one of three scopes
-- --------------------------------------------------------------------
--
-- A step''s `allowedTools` is the set of rows here at scope='step'. Granting
-- and configuring are the same row on purpose: a granted tool with nothing to
-- configure carries `config = {}`, and S4''s "every tool permitted for the
-- agent_class" check becomes one join from this table through tools to
-- capability_policy.
--
-- No secret ever lands in `config`. `secret_ref` names a Secret Manager
-- resource instead. Worth saying out loud because the project currently has
-- zero secrets in Secret Manager, so this is the first thing that will need
-- one, and a jsonb column is exactly where a token gets pasted "for now".

create table config.tool_config (
    id                       uuid        primary key default gen_random_uuid(),
    tool_code                config.code not null references config.tools (code),
    scope                    text        not null,
    step_row_id              uuid        references config.agent_version_steps (id)
                             on delete cascade,
    agent_slug               config.slug references config.agents (slug) on delete cascade,
    client_agent_config_id   uuid        references config.client_agent_config (id)
                             on delete cascade,
    config                   jsonb       not null default '{}'::jsonb,
    secret_ref               text,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now(),

    constraint tool_config_scope_vocabulary
        check (scope in ('step', 'agent', 'client_agent')),
    constraint tool_config_scope_target_agrees
        check (
            (scope = 'step'
             and step_row_id is not null
             and agent_slug is null and client_agent_config_id is null)
         or (scope = 'agent'
             and agent_slug is not null
             and step_row_id is null and client_agent_config_id is null)
         or (scope = 'client_agent'
             and client_agent_config_id is not null
             and step_row_id is null and agent_slug is null)
        ),
    constraint tool_config_secret_ref_shape
        check (secret_ref is null or secret_ref ~ '^projects/[^/]+/secrets/[^/]+')
);

create unique index tool_config_step_tool_key
    on config.tool_config (step_row_id, tool_code) where scope = 'step';
create unique index tool_config_agent_tool_key
    on config.tool_config (agent_slug, tool_code) where scope = 'agent';
create unique index tool_config_client_agent_tool_key
    on config.tool_config (client_agent_config_id, tool_code) where scope = 'client_agent';

create trigger tool_config_90_touch before update on config.tool_config
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- schedules — the two scheduling systems, merged
-- --------------------------------------------------------------------
--
-- karosCMO has two: `plannedScheduledRuns` (the firing engine, with
-- billClientCredits, timeZone, fireInFlightSince) and `scheduledRuns` behind
-- /api/scheduler, which passes `charge: null` UNCONDITIONALLY -- every one of
-- its fires is free to the client and never reaches the credit ledger.
--
-- Three things this schema refuses to inherit:
--
--   * `bill_client_credits` is NOT NULL with NO DEFAULT. The money switch has
--     to be stated. An absent flag used to fall back to an actor test while
--     createdBy stayed frozen, the two disagreed, and fires were billed to
--     the wrong party or to nobody. `billing_intent_source` records whether a
--     row''s value was stated by a human or inferred at import, so the
--     inferred ones are findable instead of indistinguishable.
--   * `time_zone` is NOT NULL. Rows written before the field existed fall
--     back to the runtime''s local zone, which means the same schedule fires
--     at a different wall-clock time depending on where the container ran.
--   * `next_run_at` is indexed for `SELECT ... FOR UPDATE SKIP LOCKED`, which
--     is the actual reason to move scheduling here: Postgres does queue
--     claiming properly, and the Firestore version needs a transaction plus
--     `fire_in_flight_since` to approximate it.

create table config.schedules (
    id                     uuid        primary key default gen_random_uuid(),
    client_slug            text        not null,
    agent_slug             config.slug not null
                           references config.agents (slug) on delete restrict,
    label                  text        not null,
    prompt                 text        not null default '',
    cadence                text        not null,
    hour                   smallint    not null,
    minute                 smallint    not null,
    weekdays               smallint[],
    day_of_month           smallint,
    time_zone              text        not null,
    outputs_per_run        integer     not null default 1,
    bill_client_credits    boolean     not null,
    billing_intent_source  text        not null,
    status                 text        not null default 'active',

    -- Run state. Deliberately in the same row as the definition: the ticket
    -- notes these are run state rather than configuration, and splitting them
    -- would mean the claim transaction touching two tables to advance one
    -- cursor.
    next_run_at            timestamptz not null,
    last_run_at            timestamptz,
    last_job_id            text,
    last_error             text,
    last_error_at          timestamptz,
    -- Non-null from the instant a fire claims its slot until the fire settles.
    -- A row still carrying it at its next claim is a fire that vanished --
    -- a container recycle between "advance the cursor" and "submit the job" --
    -- which is otherwise indistinguishable from a clean fire.
    fire_in_flight_since   timestamptz,
    fire_claim_id          uuid,

    source_system          text,
    source_id              text,
    imported_at            timestamptz,

    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    created_by             text,

    constraint schedules_client_slug_charset
        check (client_slug ~ '^[a-z0-9][a-z0-9-]*$' and length(client_slug) <= 128),
    constraint schedules_cadence_vocabulary
        check (cadence in ('daily', 'weekly', 'monthly')),
    constraint schedules_status_vocabulary
        check (status in ('active', 'paused', 'completed')),
    constraint schedules_hour_range check (hour between 0 and 23),
    constraint schedules_minute_range check (minute between 0 and 59),
    constraint schedules_outputs_per_run_positive check (outputs_per_run >= 1),
    constraint schedules_billing_intent_vocabulary
        check (billing_intent_source in ('explicit', 'inferred_at_import')),
    constraint schedules_source_system_vocabulary
        check (source_system is null
               or source_system in ('plannedScheduledRuns', 'scheduledRuns')),
    -- The cadence decides which of the two day fields must be present, and
    -- the other must be absent -- a weekly row carrying a day_of_month is a
    -- row two readers will disagree about.
    constraint schedules_cadence_fields_agree
        check (
            (cadence = 'daily'
             and weekdays is null and day_of_month is null)
         or (cadence = 'weekly'
             and weekdays is not null and array_length(weekdays, 1) between 1 and 7
             and day_of_month is null)
         or (cadence = 'monthly'
             and day_of_month between 1 and 31 and weekdays is null)
        ),
    constraint schedules_fire_claim_is_paired
        check ((fire_in_flight_since is null and fire_claim_id is null)
               or (fire_in_flight_since is not null and fire_claim_id is not null))
);

-- The draining index. Partial, because a paused schedule is never claimed and
-- there is no reason for it to sit in the queue''s index.
create index schedules_due_idx
    on config.schedules (next_run_at)
    where status = 'active';

create index schedules_client_agent_idx
    on config.schedules (client_slug, agent_slug);
create index schedules_in_flight_idx
    on config.schedules (fire_in_flight_since)
    where fire_in_flight_since is not null;

create trigger schedules_90_touch before update on config.schedules
    for each row execute function config.touch_updated_at();

-- --------------------------------------------------------------------
-- audit_log — append-only
-- --------------------------------------------------------------------
--
-- `actor` is S3''s CallerIdentity.actor and `actor_role` its resolved role, so
-- "who published this" is answerable with the same string the request was
-- authorised against.

create table config.audit_log (
    id            bigint      generated always as identity primary key,
    at            timestamptz not null default now(),
    actor         text        not null,
    actor_role    text,
    action        text        not null,
    entity_type   text        not null,
    entity_id     text        not null,
    agent_slug    config.slug,
    client_slug   text,
    before        jsonb,
    after         jsonb,
    request_id    text,
    note          text,

    constraint audit_log_action_vocabulary
        check (action in (
            'create', 'update', 'delete', 'publish', 'rollback',
            'freeze', 'restore', 'import', 'pin', 'unpin', 'enable', 'disable'
        )),
    constraint audit_log_actor_role_vocabulary
        check (actor_role is null or actor_role in ('viewer', 'editor', 'admin'))
);

create index audit_log_entity_idx on config.audit_log (entity_type, entity_id, at desc);
create index audit_log_agent_idx on config.audit_log (agent_slug, at desc);
create index audit_log_at_idx on config.audit_log (at desc);

-- =====================================================================
-- The guards
-- =====================================================================

-- --- A frozen agent version is immutable --------------------------------

create or replace function config.guard_agent_version_immutable() returns trigger
language plpgsql as $$
declare
    frozen_before jsonb;
    frozen_after  jsonb;
begin
    if tg_op = 'DELETE' then
        if old.status <> 'draft' then
            raise exception
                'agent_versions: %@v% is frozen and cannot be deleted',
                old.agent_slug, old.version
                using errcode = 'restrict_violation',
                      hint = 'Publish a new version instead. Nothing is ever removed.';
        end if;
        return old;
    end if;

    -- A draft is freely editable, including the one-way trip to frozen.
    if old.status = 'draft' then
        if new.status = 'frozen' and new.frozen_at is null then
            raise exception
                'agent_versions: %@v% cannot be frozen without frozen_at',
                old.agent_slug, old.version
                using errcode = 'restrict_violation';
        end if;
        return new;
    end if;

    -- Frozen. Nothing about it may change -- and comparing the whole row
    -- rather than a list of columns means a column added by a later
    -- migration is frozen too, which is the safe default.
    frozen_before := to_jsonb(old) - 'updated_at';
    frozen_after  := to_jsonb(new) - 'updated_at';

    if frozen_before <> frozen_after then
        raise exception
            'agent_versions: %@v% is frozen; refusing to change %',
            old.agent_slug, old.version,
            (select string_agg(key, ', ' order by key)
             from jsonb_each(frozen_after) f(key, value)
             where frozen_before -> f.key is distinct from f.value)
            using errcode = 'restrict_violation',
                  hint = 'Frozen versions never change. Publish a new version, '
                         'or move agents.published_version_id to roll back.';
    end if;

    return new;
end;
$$;

create trigger agent_versions_00_guard
    before update or delete on config.agent_versions
    for each row execute function config.guard_agent_version_immutable();

-- --- ...and so are its steps -------------------------------------------
--
-- A version whose steps can still be edited is not frozen; it just looks
-- frozen from the parent row. This is the guard the ticket''s one-line
-- description hides.

create or replace function config.guard_frozen_version_steps() returns trigger
language plpgsql as $$
declare
    target_version uuid;
    parent_status  text;
    parent_slug    config.slug;
    parent_number  integer;
begin
    target_version := coalesce(new.version_id, old.version_id);

    select v.status, v.agent_slug, v.version
      into parent_status, parent_slug, parent_number
      from config.agent_versions v
     where v.id = target_version;

    -- The parent is already gone: this is the ON DELETE CASCADE of a draft
    -- version, which the parent guard has already vetted.
    if not found then
        return coalesce(new, old);
    end if;

    if parent_status <> 'draft' then
        raise exception
            'agent_version_steps: %@v% is frozen; refusing to % a step',
            parent_slug, parent_number, lower(tg_op)
            using errcode = 'restrict_violation',
                  hint = 'Copy the version to a new draft and edit that.';
    end if;

    return coalesce(new, old);
end;
$$;

create trigger agent_version_steps_00_guard
    before insert or update or delete on config.agent_version_steps
    for each row execute function config.guard_frozen_version_steps();

-- --- ...and the tools granted to those steps ---------------------------

create or replace function config.guard_frozen_step_tool_config() returns trigger
language plpgsql as $$
declare
    target_step   uuid;
    parent_status text;
begin
    target_step := coalesce(new.step_row_id, old.step_row_id);
    if target_step is null then
        return coalesce(new, old);   -- agent or client scope: not frozen
    end if;

    select v.status into parent_status
      from config.agent_version_steps s
      join config.agent_versions v on v.id = s.version_id
     where s.id = target_step;

    if not found then
        return coalesce(new, old);   -- cascade from a deleted draft step
    end if;

    if parent_status <> 'draft' then
        raise exception
            'tool_config: step % belongs to a frozen version; refusing to % it',
            target_step, lower(tg_op)
            using errcode = 'restrict_violation';
    end if;

    return coalesce(new, old);
end;
$$;

create trigger tool_config_00_guard
    before insert or update or delete on config.tool_config
    for each row execute function config.guard_frozen_step_tool_config();

-- --- A prompt version is append-only -----------------------------------
--
-- No exception for `notes`. The whole complaint against the current
-- engine-prompts route is that content can be replaced in place; an
-- editable column on a version row is the shape that grows back into that.

create or replace function config.guard_prompt_version_append_only() returns trigger
language plpgsql as $$
begin
    raise exception
        'prompt_versions: append-only; % is not permitted', lower(tg_op)
        using errcode = 'restrict_violation',
              hint = 'Write a new version. To reinstate an old one, insert a '
                     'version with its content and set restored_from_version_id.';
end;
$$;

create trigger prompt_versions_00_guard
    before update or delete on config.prompt_versions
    for each row execute function config.guard_prompt_version_append_only();

-- --- The audit log is append-only --------------------------------------

create or replace function config.guard_audit_log_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_log: append-only; % is not permitted', lower(tg_op)
        using errcode = 'restrict_violation';
end;
$$;

create trigger audit_log_00_guard
    before update or delete on config.audit_log
    for each row execute function config.guard_audit_log_append_only();

-- --- A live pointer must point at a frozen version ---------------------
--
-- Pointing an agent at a draft would put an editable version on the run
-- path, which is the entire failure mode the freeze exists to prevent.

create or replace function config.guard_pointer_targets_frozen() returns trigger
language plpgsql as $$
declare
    target_status text;
    target_id     uuid;
    column_name   text;
begin
    if tg_table_name = 'agents' then
        target_id := new.published_version_id;
        column_name := 'published_version_id';
    else
        target_id := new.pinned_version_id;
        column_name := 'pinned_version_id';
    end if;

    if target_id is null then
        return new;
    end if;

    select status into target_status
      from config.agent_versions where id = target_id;

    if target_status <> 'frozen' then
        raise exception
            '%.%: version % is a draft; only a frozen version can be live',
            tg_table_name, column_name, target_id
            using errcode = 'restrict_violation';
    end if;

    return new;
end;
$$;

create trigger agents_10_pointer_guard
    before insert or update of published_version_id on config.agents
    for each row execute function config.guard_pointer_targets_frozen();

create trigger client_agent_config_10_pointer_guard
    before insert or update of pinned_version_id on config.client_agent_config
    for each row execute function config.guard_pointer_targets_frozen();

-- --- A step must satisfy its kind''s declared requirements --------------
--
-- Driven by the four `requires_*` flags on step_kinds rather than by a CHECK
-- per kind, so registering a new kind is an INSERT and not a migration.

create or replace function config.guard_step_matches_kind() returns trigger
language plpgsql as $$
declare
    k config.step_kinds;
begin
    select * into k from config.step_kinds where code = new.kind_code;

    if k.requires_prompt and new.prompt_version_id is null then
        raise exception 'step %: kind "%" requires a prompt version',
            new.step_id, new.kind_code using errcode = 'not_null_violation';
    end if;

    if k.requires_code and (new.code is null or new.language is null) then
        raise exception 'step %: kind "%" requires code and a language',
            new.step_id, new.kind_code using errcode = 'not_null_violation';
    end if;

    if k.requires_output_schema
       and (new.output_schema is null or jsonb_array_length(
                case when jsonb_typeof(new.output_schema) = 'array'
                     then new.output_schema else '[]'::jsonb end) = 0) then
        raise exception 'step %: kind "%" requires a non-empty output schema',
            new.step_id, new.kind_code using errcode = 'not_null_violation';
    end if;

    if k.requires_gate_kind and new.gate_kind is null then
        raise exception 'step %: kind "%" requires a gate kind',
            new.step_id, new.kind_code using errcode = 'not_null_violation';
    end if;

    return new;
end;
$$;

create trigger agent_version_steps_20_kind_guard
    before insert or update on config.agent_version_steps
    for each row execute function config.guard_step_matches_kind();

insert into config.schema_migrations (filename) values ('0001_config_plane.sql');

commit;
