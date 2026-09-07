# C4 · Per-agent capability descriptor

| | |
|---|---|
| **Status** | Draft for approval by Shlomi and Tomer |
| **Jira** | SCRUM-212 · depends on S9 (SCRUM-226, fixed) |
| **Seam** | Shlomi populates (`agent-middleware`) → Tomer consumes (`karosCMO`) |
| **Producer** | S-A16 (SCRUM-230) — the catalog in the control plane |
| **Consumers** | T-B6 chat router, T-B7 agent selection, T-B22 task generators, T-B10 typed task metadata |
| **Verified against** | `agent-middleware` @ `9910dc0` · `agent-engine` @ `89bb8c4` · `karosCMO` @ `fe9b5f2` |

---

## 1. The problem in one line

The control plane already holds `required_inputs`, `stages`, `tags` and
`category`. The portal fetches them and does not consume them: `requiredInputs`
appears at exactly two production sites, both inside
`src/lib/agent-engine/middleware-admin.ts` — the file that fetches it. The
planner receives `capabilities: []` from both of its production sites in
`src/lib/agent-roster.ts`, and agent selection is a substring match on the
display name.

## 2. The ownership principle

> **The descriptor is derived from the engine and stored in the control plane.
> The portal consumes it and never hand-writes it.**

What is auto-derived does not go stale; what is hand-written in the portal
does. Fields that cannot be derived — `platforms`, the semantic `capabilities`
— are written once in the middleware catalog, which is already the source of
truth for agent identity.

§7 makes that split concrete per field, because "derived" is a promise about a
mechanism and three of the fields do not have one yet.

## 3. Four sources disagree about which agents exist

This is the finding that shapes the rest of the contract. Four places in the
three repositories enumerate "the agents", and **no two of them contain the same
set**:

| Source | Count | What it holds |
|---|---|---|
| `agent-engine` `KNOWN_PRODUCT_IDS` | **13** | the 12 below **plus `campaign-orchestrator`** |
| `agent-middleware` `seed_all_agents.py` `CATALOG` | **12** | no `campaign-orchestrator` |
| `report_client_readiness.py` `AGENT_REQUIREMENTS` | **14** | the 12 **plus `linkedin-setup-agent` and `reddit-setup-agent`** |
| `karosCMO` `ENGINE_PRODUCT_BY_CUSTOM_AGENT_KEY` | **14 keys → 12 slugs** | two portal keys each for linkedin and reddit |

Two of those gaps are bugs rather than differences of opinion:

* **The readiness report still scores two agents that no longer exist.**
  `linkedin-setup-agent` and `reddit-setup-agent` were retired from the catalog
  (`e165597`) when their workflow was inlined into each parent as its
  `00-channel-setup` pre-flight. `AGENT_REQUIREMENTS` still has a row for each,
  so every readiness run reports on 14 products, two of which cannot be
  dispatched. Their requirements are not wrong — they moved into the parents.
* **`campaign-orchestrator` is routable in the engine and absent from the
  catalog.** It is in `KNOWN_PRODUCT_IDS` and has 15 stages recorded in
  `engine_stages.json`, and the middleware has no agent row for it, so it can
  have no descriptor.

**The engine's `KNOWN_PRODUCT_IDS` is the identity set.** A slug is a real
agent when the engine can dispatch it; everything else is a view. The other
three lists reconcile to it, and §8.1 makes that a test rather than an
intention.

## 4. The shape

```jsonc
// GET /agents/{slug}/descriptor — or a field on AgentRead; served by the control plane
{
  "slug": "x-agent",                      // = engine productId. THE identity.
  "customAgentKeys": ["karos-x-agent-v2"], // portal keys that route here — a LIST, see §5
  "name": "X Agent",
  "description": "…",
  "category": "social",
  "tags": ["social", "x", "draft-only"],
  "creditCost": 6,
  "isPublic": true,
  "status": "enabled",                    // "enabled" | "disabled" | "legacy_only"

  "capabilities": ["draft_social_post"],  // controlled vocabulary — §6
  "platforms": ["x"],
  "consumesMedia": false,                 // is mediaAssets relevant to this agent
  "supportsTargetDate": true,             // is C3's targetDate honoured

  "requiredInputs": [                     // the existing AgentInputDef — not a new schema
    { "key": "requestedLane", "type": "select", "label": "Lane",
      "required": false, "options": ["founder", "company"] }
  ],

  "gates": ["batch_review"],              // human pauses on the path — §7.3
  "readiness": {
    "hard": ["client/profile", "client/config:xHandle", "strategy/x-agent"],
    "soft": ["topics/catalog"]
  }
}
```

## 5. `customAgentKeys` is a list, and that is not a style choice

The draft of this contract had a single `customAgentKey`. The map it is meant to
replace is **not** one-to-one:

```
karos-x-agent-v2          → x-agent
karos-linkedin-writer-v2  → linkedin-agent
karos-linkedin-setup-v2   → linkedin-agent     ← two keys
karos-reddit-runner       → reddit-agent
karos-reddit-setup        → reddit-agent       ← two keys
…
```

Both pairs exist for the same recorded reason: the setup workflow was inlined
into its parent, so a run dispatched from a lab setup key carries the same
filled form it always did and the parent records it, skips it if a charter
already exists, and drafts. Fourteen portal keys route to twelve engine
products.

A single-valued field cannot express that, so the consistency test invariant 1
demands — descriptor mapping ≡ the hard-coded map — could never pass, and
whoever implemented it would either drop a key or invent a second descriptor for
the same slug. Hence `customAgentKeys: string[]`, with the constraint that the
union across all descriptors contains each portal key exactly once.

## 6. The `capabilities` vocabulary — controlled and small

Verbs, not descriptions. A closed list, extended only by PR:

```
draft_social_post · draft_article · draft_newsletter · draft_reply
build_landing_page · produce_video · produce_carousel
run_seo_audit · run_intel_report · run_setup · orchestrate_campaign
```

The router chooses on `capabilities` × `platforms` × `consumesMedia` — never on
the name. A request for video against an agent with no `produce_video` is
refused with an explanation, not attempted.

`run_setup` stays in the vocabulary although both setup agents were retired: the
capability did not disappear, it moved inside the drafting agents as
`00-channel-setup`. An agent that can absorb an intake form declares it.

## 7. Where each field comes from

The ownership principle is only as good as the mechanism behind the word
"derived". Three tiers:

### 7.1 Already present in the control plane — nothing to build

`slug`, `name`, `description`, `category`, `tags`, `creditCost`, `isPublic`,
`requiredInputs`, `status`. All nine are fields on the agent document, all nine
are populated for the 12 catalog agents by `seed_all_agents.py`, and as of S9
(`07da704`) all nine survive a `POST /agents` as well as a `PATCH`.

Note for whoever writes S-A16: `requiredInputs` is **already populated for all
twelve agents** — `request`, `requestedLane`, `requestedSubreddit`,
`requestedThreadUrl`, `requestedIdentityScope`, `sourcePath`. SCRUM-230 is
phrased as though the field were empty. It is not; the work is the semantic
fields below, plus reconciling §3.

### 7.2 Hand-written once, in the middleware catalog

`capabilities`, `platforms`, `consumesMedia`, `supportsTargetDate`,
`customAgentKeys`. None is derivable from the workflow source — they describe
what an agent is *for*, which is a product statement. They live in the catalog
beside the identity they qualify, they change by PR, and a test asserts the
vocabulary (§8.2).

### 7.3 `gates` — not derivable today, and the reason matters

Invariant 4 exists because a planner promising "you will get a post now" must
know a `batch_review` sits in the middle. Deriving it from `engine_stages.json`
is the obvious move and it produces the wrong answer:

**`generate_engine_stages.py` never emits `is_gate` at all.** Each of the 250
recorded stages, across all 15 products, carries exactly `id`, `kind` and
sometimes `skill_ref` — so every stage validates to `AgentStage`'s default,
`is_gate: false`.

The gates are real. Nine agents declare one — `batch_review` in blog, instagram,
intel-report, linkedin, newsletter, reddit, tiktok and x; `prompt_set_review`
and `fix_generation_review` in seo-geo. `generate_engine_stages.py` cannot see
them because it matches `wf.step.(code|agent|gate)(` by regex, and those nine
declare their gate as a `buildGate:` callback handed to the shared review-cycle
helper rather than as a literal `wf.step.gate(` call. Only
`campaign-orchestrator` calls it literally, and it is the only product in
`engine_stages.json` carrying `kind: "gate"`.

So, one of two things, decided before S-A16:

**(a)** Extend the generator to recognise `buildGate:` and populate `is_gate`
and `gates` from it. Correct, and it keeps gates on the derived side of the
ownership principle where they belong.
**(b)** Hand-write `gates` in the catalog as a §7.2 field, with a test asserting
each named gate kind appears somewhere in that agent's workflow source.

**(a) is the recommendation**, and it should be its own ticket in `agent-engine`
or in `generate_engine_stages.py`. What is *not* acceptable is populating
`gates` from `is_gate` as it stands: every descriptor would carry `gates: []`,
the planner would promise immediate delivery for nine agents that pause for a
human, and the invariant would be satisfied on paper by a field that is
uniformly wrong.

### 7.4 `readiness` — derived from `AGENT_REQUIREMENTS`

`report_client_readiness.py` already holds `hard` and `soft` per agent, already
distinguishes the two ("calling a soft gap blocked makes the report useless by
crying wolf"), and already records who is meant to supply each missing path.
The descriptor exposes that structure; it does not restate it. Its two phantom
rows (§3) are removed as part of the same work.

## 8. Invariants

1. **`slug` is the identity.** `customAgentKeys → slug` is data in the
   descriptor and gradually replaces the hard-coded portal map. Transitionally,
   a test asserts the two are identical, and it can only be written once
   `customAgentKeys` is a list (§5).
2. **An agent with no descriptor is not routable from chat.** No fallback to the
   name. This is what retires
   `customAgents.find(a => a.name.toLowerCase().includes(q))`.
3. `requiredInputs` comes from the fixed `POST /agents` (S9). Closed as of
   `07da704`.
4. **The descriptor includes `gates`,** subject to §7.3. A descriptor whose
   `gates` are uniformly empty does not satisfy this invariant, it evades it.
5. **The five portal agents with no engine counterpart get
   `status: "legacy_only"`** — `karos-carousel-setup`, `karos-carousel-runner`,
   `karos-carousel-manager`, `karos-linkedin-manager-v2`,
   `karos-reputation-manager`. **Five, not four**: the earlier draft said four
   and then listed five. The router says "that is still on the old path" instead
   of failing. `karos-linkedin-manager-v2` is the documented case — it runs on
   two clocks and rewrites the generators' inputs, and the engine has neither a
   scheduler nor a write path for that.
6. **The vocabulary is closed.** A capability outside §6 is a build failure, not
   a string.

## 9. Acceptance criteria

1. Every slug in `KNOWN_PRODUCT_IDS` has a descriptor; a test fails on one
   without. That includes `campaign-orchestrator`, which needs a catalog row
   first.
2. `AGENT_REQUIREMENTS`, the catalog and `KNOWN_PRODUCT_IDS` name the same set;
   a test compares all three. The two retired setup agents are gone from the
   readiness report.
3. The union of `customAgentKeys` across descriptors equals the key set of
   `ENGINE_PRODUCT_BY_CUSTOM_AGENT_KEY`, exactly, with no key appearing twice.
   The test survives until the portal map is deleted.
4. The planner's prompt is built from descriptors, with zero knowledge of any
   specific agent in portal code.
5. The router refuses a video request to an agent lacking `produce_video`, with
   an explanation.
6. A capability string outside §6 fails the build.
7. `gates` is non-empty for the nine agents that have one, by whichever of
   §7.3's routes is chosen.

## 10. What this contract does not decide

Whether `campaign-orchestrator` should be client-routable at all, or remains an
internal composition step. It needs a catalog row either way — a descriptor with
`isPublic: false` is a perfectly good answer, and it is a product call rather
than a schema one.
