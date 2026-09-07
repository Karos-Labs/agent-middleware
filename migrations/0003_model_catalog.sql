-- =====================================================================
-- 0003_model_catalog.sql — the model catalog, priced (SCRUM-222 / S12)
--
-- The same rows scripts/seed_model_catalog.py writes to Firestore, for the
-- Postgres catalog S2 created. Two stores hold this during the migration
-- window, deliberately: Firestore is on the run path today, Postgres becomes
-- the editing surface at S4/S5, and nothing about the run path changes until
-- then. S5's importer is what retires the duplication.
--
-- APPLIES AFTER 0001 and 0002, and this file therefore merges after the S2
-- branch. Ordering is not cosmetic: config.models does not exist before 0001.
--
-- Every price below was read off the vendor's own price list on 2026-09-04.
-- Three of the numbers the platform currently uses are wrong, which is a
-- stronger finding than the ticket's ("a miss falls back to Sonnet"):
--
--   claude-opus-4-8    hard-coded $15/$75   published $5/$25   3x overstated
--   claude-opus-4-7    hard-coded $15/$75   published $5/$25   3x overstated
--   claude-haiku-4-5   hard-coded $0.80/$4  published $1/$5    understated
--
-- Idempotent: ON CONFLICT DO UPDATE, so re-applying after a price change is
-- how a price change is applied.
-- =====================================================================

begin;

set local search_path = config, public;

insert into config.models (
    model_id, display_name, vendor, route, availability, provider_model_name,
    region, description, context_window, supports_tools, tiers,
    input_per_1m, output_per_1m, cached_input_per_1m,
    pricing_source, pricing_checked_on, notes
) values
    -- --- Anthropic, through Vertex / Agent Platform ----------------------
    ('claude-sonnet-4-6-on-vertex', 'Claude Sonnet 4.6 (Vertex)',
     'anthropic', 'anthropic', 'available', 'claude-sonnet-4-6', 'global',
     'The default drafting model for every hand-written agent in agent-engine.',
     200000, true, array['pinned', 'portable'],
     3.0, 15.0, 0.30,
     'platform.claude.com/docs/en/about-claude/pricing', '2026-09-04', null),

    ('claude-haiku-4-5-on-vertex', 'Claude Haiku 4.5 (Vertex)',
     'anthropic', 'anthropic', 'available', 'claude-haiku-4-5-20251001', 'global',
     'Classification and gating. What the dynamic runner''s topic-guardrail verifier uses.',
     200000, true, array['commodity'],
     1.0, 5.0, 0.10,
     'platform.claude.com/docs/en/about-claude/pricing', '2026-09-04',
     'Both hard-coded tables carry $0.80/$4.00. The published price is $1/$5, so '
     'guardrail and classification spend has been understated by a quarter.'),

    ('claude-opus-5-on-vertex', 'Claude Opus 5 (Vertex)',
     'anthropic', 'anthropic', 'not_enabled', 'claude-opus-5', 'global',
     'Current highest-capability Anthropic tier.',
     200000, true, array['pinned'],
     5.0, 25.0, 0.50,
     'platform.claude.com/docs/en/about-claude/pricing', '2026-09-04',
     'Absent from both pricing tables, so any run on it was costed at the Sonnet '
     'fallback.'),

    ('claude-sonnet-5-on-vertex', 'Claude Sonnet 5 (Vertex)',
     'anthropic', 'anthropic', 'not_enabled', 'claude-sonnet-5', 'global',
     'Current Sonnet generation.',
     200000, true, array['pinned', 'portable'],
     2.0, 10.0, 0.20,
     'platform.claude.com/docs/en/about-claude/pricing', '2026-09-04',
     '$2/$10 is what the price list showed on the checked date. At least one '
     'secondary source reports it as an introductory rate reverting to $3/$15 -- '
     're-check before quoting it.'),

    ('claude-opus-4-8-on-vertex', 'Claude Opus 4.8 (Vertex)',
     'anthropic', 'anthropic', 'not_enabled', 'claude-opus-4-8', 'global',
     'Previous highest-capability Anthropic tier.',
     200000, true, array['pinned'],
     5.0, 25.0, 0.50,
     'platform.claude.com/docs/en/about-claude/pricing', '2026-09-04',
     'Both hard-coded tables carry $15/$75 -- three times the published price. Every '
     'Opus step in every cost report is overstated threefold until they are fixed.'),

    -- --- Google -----------------------------------------------------------
    ('gemini-2-5-pro', 'Gemini 2.5 Pro',
     'google', 'gemini', 'available', 'gemini-2.5-pro', 'us-central1',
     'Long-context reasoning. Wired through the same Vertex router as Claude.',
     1000000, true, array['portable'],
     1.25, 10.0, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04', null),

    ('gemini-2-5-flash', 'Gemini 2.5 Flash',
     'google', 'gemini', 'available', 'gemini-2.5-flash', 'us-central1',
     'Cheap, fast extraction and classification.',
     1000000, true, array['commodity'],
     0.30, 2.50, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04',
     'Text/image/video input. Audio input is priced separately at $1.00/1M.'),

    ('gemini-2-5-flash-lite', 'Gemini 2.5 Flash Lite',
     'google', 'gemini', 'available', 'gemini-2.5-flash-lite', 'us-central1',
     'The cheapest token-priced model on the platform.',
     1000000, true, array['commodity'],
     0.10, 0.40, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04',
     'Seeded so the engine''s tertiary fallback can move off gemini-1.5-flash, which '
     'Vertex prices per character and nothing can cost honestly.'),

    ('gemini-1-5-flash', 'Gemini 1.5 Flash',
     'google', 'gemini', 'available', 'gemini-1.5-flash', 'us-central1',
     'agent-engine''s TERTIARY fallback -- the one hop that changes model identity.',
     1000000, true, array['commodity'],
     0.075, 0.30, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing -- Vertex prices this model PER '
     '1,000 CHARACTERS ($0.00001875 in / $0.000075 out), not per token. Converted at '
     'Google''s own 4-characters-per-token guidance; approximate by construction.',
     '2026-09-04',
     'The engine assumed $3/$15 here, so its fallback hops were costed 40x and 50x '
     'over. Preferred fix: point CLAUDE_FALLBACK_GEMINI_MODEL at '
     'gemini-2.5-flash-lite, which is token-priced.'),

    -- --- Model Garden partner models --------------------------------------
    ('llama-3-3-70b-instruct-maas', 'Llama 3.3 70B Instruct (MaaS)',
     'meta', 'model-garden', 'not_enabled', 'meta/llama-3.3-70b-instruct-maas',
     'us-central1', 'Open-weights option served through Vertex Model-as-a-Service.',
     128000, false, array['commodity'],
     0.72, 0.72, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04',
     'Replaces llama-3-1-70b-instruct-maas, for which Google publishes no price. Note '
     'supports_tools is false: a stage granting tools would not work on it even once '
     'enabled.'),

    ('mistral-small-2503', 'Mistral Small 3.1 (25.03)',
     'mistral', 'model-garden', 'not_enabled', 'mistral-small-2503', 'us-central1',
     'Cheap open-weights alternative for commodity steps.',
     128000, false, array['commodity'],
     0.10, 0.30, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04', null),

    ('mistral-medium-3', 'Mistral Medium 3',
     'mistral', 'model-garden', 'not_enabled', 'mistral-medium-3', 'us-central1',
     'Mid-tier open-weights option.',
     128000, false, array['portable'],
     0.40, 2.00, null,
     'cloud.google.com/vertex-ai/generative-ai/pricing', '2026-09-04', null)

on conflict (model_id) do update set
    display_name        = excluded.display_name,
    vendor              = excluded.vendor,
    route               = excluded.route,
    availability        = excluded.availability,
    provider_model_name = excluded.provider_model_name,
    region              = excluded.region,
    description         = excluded.description,
    context_window      = excluded.context_window,
    supports_tools      = excluded.supports_tools,
    tiers               = excluded.tiers,
    input_per_1m        = excluded.input_per_1m,
    output_per_1m       = excluded.output_per_1m,
    cached_input_per_1m = excluded.cached_input_per_1m,
    pricing_source      = excluded.pricing_source,
    pricing_checked_on  = excluded.pricing_checked_on,
    notes               = excluded.notes,
    updated_at          = now();

-- --------------------------------------------------------------------
-- The Studio's three-option picker
-- --------------------------------------------------------------------
--
-- Matches agent-engine's MODEL_ALIASES exactly, so moving resolution here
-- changes nothing about what runs.
--
-- `opus` is worth a second look: the engine points it at claude-opus-4-8 and
-- marks it pinned, while the catalog marks that model not_enabled -- so the
-- alias resolves to a model this deployment does not route. Preserved as-is
-- rather than quietly repointed: the two lists disagreeing is a finding, and
-- fixing it is either enabling the model or changing the alias, both of which
-- are decisions someone should make on purpose.

insert into config.model_aliases (alias, model_id, provider_policy, description) values
    ('haiku', 'claude-haiku-4-5-on-vertex', 'commodity',
     'Classification, extraction, sorting, dedupe similarity.'),
    ('sonnet', 'claude-sonnet-4-6-on-vertex', 'pinned',
     'The default for writing and judgment -- reaches a client, stays pinned.'),
    ('opus', 'claude-opus-4-8-on-vertex', 'pinned',
     'Reserved for when exact phrasing is the deliverable itself.')
on conflict (alias) do update set
    model_id        = excluded.model_id,
    provider_policy = excluded.provider_policy,
    description     = excluded.description,
    updated_at      = now();

-- A price is only as good as its date, and a row that carries neither is the
-- row this ticket exists to remove. Belt and braces over the NOT NULL columns:
-- this catches a future insert that satisfies the constraint with a placeholder.
do $$
declare
    stale integer;
begin
    select count(*) into stale
      from config.models
     where pricing_checked_on < current_date - interval '400 days';
    if stale > 0 then
        raise warning
            'config.models: % row(s) carry a price checked over 400 days ago', stale;
    end if;
end $$;

insert into config.schema_migrations (filename) values ('0003_model_catalog.sql')
on conflict (filename) do nothing;

commit;
