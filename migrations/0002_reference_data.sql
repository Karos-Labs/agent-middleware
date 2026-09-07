-- =====================================================================
-- 0002_reference_data.sql — the vocabularies the schema needs to be usable
--
-- Only what is DERIVED from something that already exists. Specifically NOT
-- here:
--
--   * `models`, `model_aliases` — S12 (SCRUM-222) moves MODEL_ALIASES and the
--     17 MODEL_PRICING rows out of the engine, and doing it here would mean
--     two places to update in the same week.
--   * `tools` — the engine's AgentToolRegistry is code, not data. S4 populates
--     it as part of making the publish check real.
--
-- Idempotent: every insert is ON CONFLICT DO NOTHING, so re-applying is a
-- no-op rather than an error.
-- =====================================================================

begin;

set local search_path = config, public;

-- --------------------------------------------------------------------
-- step_kinds — the three the engine actually has
-- --------------------------------------------------------------------
--
-- Counted from scripts/engine_stages.json: 250 stages across 15 products,
-- 216 code / 33 agent / 1 gate. The single literal gate is
-- campaign-orchestrator's; the other nine agents declare theirs as a
-- `buildGate:` callback the generator cannot see (C4 §7.3), which is why
-- `is_gate` on a step is authored rather than derived for now.

insert into config.step_kinds (
    code, engine_alias, display_name, description,
    requires_prompt, requires_code, requires_output_schema, requires_gate_kind
) values
    ('ai', 'agent', 'AI stage',
     'One BaseAgent ReAct loop. The default: a stored stage with no kind '
     'parses as this one. Needs a prompt and a declared output schema, '
     'because a model turn whose shape is not declared cannot be validated.',
     true, false, true, false),

    ('code', null, 'Code stage',
     'A deterministic transform, run out-of-process by the dynamic sandbox. '
     'Exists because reformatting a date or picking the top three of a list '
     'should be exact, and paying a model to do it buys variance rather than '
     'judgment. The script is untrusted input.',
     false, true, false, false),

    ('gate', null, 'Human gate',
     'A pause for a person. Not a step that produces content -- a step that '
     'refuses to continue until someone approves. A planner that does not '
     'know one of these sits in the middle promises delivery it cannot make.',
     false, false, false, true)
on conflict (code) do nothing;

-- --------------------------------------------------------------------
-- agent_classes — derived from C4 §6's closed capability vocabulary
-- --------------------------------------------------------------------
--
-- Not invented: each class is exactly one group of the eleven capabilities
-- C4 §6 fixes, which is the only closed vocabulary in the three repositories
-- that describes what an agent is FOR. The portal's `category` (social / web
-- / video / research / content / reputation / seo) is not a substitute -- it
-- groups agents for display, and two agents in the same category can want
-- very different tools.

insert into config.agent_classes (code, display_name, description) values
    ('drafting', 'Drafting',
     'Produces text a human reviews before it reaches an audience. Every one '
     'of these is draft-first, which is why the class exists separately from '
     'anything that publishes.'),
    ('web_build', 'Web build',
     'Produces a page or a site artifact rather than a post.'),
    ('media', 'Media production',
     'Produces video or image sequences. The one class that legitimately '
     'consumes mediaAssets.'),
    ('analysis', 'Analysis',
     'Reads and reports: audits, intel reports. Produces findings, not '
     'publishable content, and needs read-heavy tooling nothing else does.'),
    ('setup', 'Setup / intake',
     'Absorbs an intake form into a charter. Both standalone setup agents '
     'were retired and the capability moved inside the drafting agents as '
     'their 00-channel-setup pre-flight, so this class describes a phase '
     'rather than a product.'),
    ('orchestration', 'Orchestration',
     'Composes other agents. campaign-orchestrator is the only member, and '
     'whether it is client-routable at all is still an open product call.')
on conflict (code) do nothing;

-- --------------------------------------------------------------------
-- capability_policy — which class may declare which capability
-- --------------------------------------------------------------------
--
-- Default deny, so this table is the whole of what is permitted. Tool grants
-- are absent on purpose: `tools` is empty until S4 populates it, and a policy
-- row naming a tool that does not exist yet would fail its own foreign key.

insert into config.capability_policy (agent_class_code, subject_type, subject) values
    ('drafting',      'capability', 'draft_social_post'),
    ('drafting',      'capability', 'draft_article'),
    ('drafting',      'capability', 'draft_newsletter'),
    ('drafting',      'capability', 'draft_reply'),
    ('drafting',      'capability', 'run_setup'),
    ('web_build',     'capability', 'build_landing_page'),
    ('media',         'capability', 'produce_video'),
    ('media',         'capability', 'produce_carousel'),
    ('analysis',      'capability', 'run_seo_audit'),
    ('analysis',      'capability', 'run_intel_report'),
    ('setup',         'capability', 'run_setup'),
    ('orchestration', 'capability', 'orchestrate_campaign'),
    -- Model tiers, per class. `pinned` everywhere a client sees the words:
    -- a drafting step that silently swapped models would change brand voice
    -- without anyone deciding to.
    ('drafting',      'model_tier', 'pinned'),
    ('web_build',     'model_tier', 'pinned'),
    ('media',         'model_tier', 'pinned'),
    ('analysis',      'model_tier', 'pinned'),
    ('analysis',      'model_tier', 'portable'),
    ('analysis',      'model_tier', 'commodity'),
    ('setup',         'model_tier', 'portable'),
    ('orchestration', 'model_tier', 'portable')
on conflict (agent_class_code, subject_type, subject) do nothing;

insert into config.schema_migrations (filename) values ('0002_reference_data.sql')
on conflict (filename) do nothing;

commit;
