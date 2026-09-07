# C6 · ExecutionSnapshot — the wire shape

| | |
|---|---|
| **Status** | Draft for approval by Shlomi and Tomer |
| **Jira** | SCRUM-214 · depends on S2 (SCRUM-217, done) · blocks S6 (SCRUM-219) |
| **Seam** | Shlomi produces (`agent-middleware`) → Tomer consumes (`agent-engine`) |
| **Producer** | S6 (SCRUM-219) — the resolver in the control plane |
| **Consumers** | `agent-server`'s `startRunJob` and every `step.agent` / `step.code` call under it |
| **Verified against** | `agent-middleware` @ `911cd4b` (the S2 schema) · `agent-engine` @ `89bb8c4` · `karosCMO` @ `fe9b5f2` |

Written after S2 on purpose: the snapshot's shape is the schema's shape, and
drafting it first would have meant guessing. It is also the one wire contract
that is expensive to change later — a change lands while runs are already
sitting in the queue carrying the old shape.

Amendable until S6 merges. After that, a change needs both sides deployed in
the same window, and a `schemaVersion` bump.

---

## 1. The principle

> A run carries the configuration it will use. The engine resolves nothing at
> run time, and the control plane's database is not on the run path.

Everything else in this document follows from that sentence. If the engine
resolves anything — a prompt body, a model id, a tool list — then editing that
thing changes a run already in flight, and "which configuration produced this
deliverable" is answerable only by reconstructing what the database happened to
contain at the time.

## 2. What changes on the run path

Today, dispatch publishes a message naming an agent, and the engine assembles
the rest as it goes:

```
karosCMO → POST /agents/{id}/jobs → middleware → Pub/Sub → agent-server
                                                              │
                          ┌───────────────────────────────────┤
                          ▼                                   ▼
              PromptStore.getPrompt(skillRef)      AgentDefinitionStore.get(productId)
              (Firestore / Vertex / file)          (Firestore)
```

Three things are wrong with that, and they are the same thing three times:

* A prompt edited between dispatch and execution changes a run nobody was told
  about. The `engine-prompts` route makes this worse than a race — it
  *overwrites content in place* (S7), so the old body is not merely superseded,
  it is gone.
* A queued backlog is a set of runs whose configuration is still moving. A gate
  that pauses for three days resumes against whatever the database says on day
  three.
* Cost attribution reads the model id off the *result*, and prices it against a
  hard-coded table that silently falls back to Sonnet's $3/$15 on a miss (S12).

After:

```
karosCMO → POST /agents/{id}/jobs → middleware
                                        │  resolve published-or-pinned version
                                        │  → freeze into an ExecutionSnapshot
                                        ▼
                                     Pub/Sub → agent-server → reads the snapshot
```

The engine keeps its prompt stores and its definition store for the paths that
still need them (local development, a dynamic agent registered directly through
`POST /api/agents`). It stops using them for a run that arrives with a
snapshot, and §8 makes that a fallback with a name rather than an accident.

## 3. The shape

`snapshot` is a new optional field on the existing `RunJobRequestSchema`, so
nothing that publishes today breaks. Its `schemaVersion` is the snapshot's own
and independent of the message envelope's.

```jsonc
{
  // --- the existing message, unchanged ---
  "clientSlug": "geektime",
  "productId": "blog-agent",
  "runKind": "scheduled",
  "input": { "customPrompt": "…", "mediaAssets": [] },

  // --- new, and exactly one of these two ---
  "snapshot": {
    "schemaVersion": 1,

    "snapshotId": "6d1e2f3a-…",        // stable id; recorded on the run
    "agentSlug": "blog-agent",          // == productId. Cross-checked, see §7.2
    "agentVersionId": "3333…",          // config.agent_versions.id
    "agentVersion": 12,                 // the human-readable number
    "resolvedAt": "2026-09-04T07:41:00Z",
    "resolvedFrom": "published",        // "published" | "client_pinned" — §5
    "clientSlug": "geektime",
    "agentClass": "drafting",
    "capabilities": ["draft_article"],

    "defaults": {
      "modelId": "claude-sonnet-4-6",
      "providerPolicy": "pinned",
      "agentStepTimeoutMs": 600000,     // per-RUN, not per-step — §9.1
      "dedupeAgainstHistory": false
    },

    "steps": [
      {
        "stepId": "10-draft-post",
        "position": 0,
        "kind": "ai",                   // "ai" | "code" | "gate"
        "description": "Draft the post from the strategy and the brief.",

        // Present for kind "ai". The BODY travels, not a reference.
        "prompt": {
          "promptKey": "blog-agent/10-draft-post",
          "promptVersionId": "0f9b…",
          "version": 7,
          "contentHash": "…64 hex…",    // sha256 of content — §7.4
          "content": "You are drafting…"
        },

        // A model resolved all the way down. No alias, no lookup.
        "model": {
          "modelId": "claude-sonnet-4-6",
          "providerModelName": "claude-sonnet-4-6",
          "vendor": "anthropic",        // who makes it
          "route": "anthropic",         // how this deployment reaches it — §9.2
          "region": "us-east5",
          "providerPolicy": "pinned",
          "fallbackModel": null,        // null whenever providerPolicy is "pinned"
          "pricing": {                  // the row in effect — §7.5
            "inputPer1M": 3.0,
            "outputPer1M": 15.0,
            "cachedInputPer1M": null
          }
        },

        "allowedTools": [
          { "name": "read_client_context", "version": "1.0.0", "config": {} }
        ],

        "outputSchema": [               // the flat DSL, as authored
          { "name": "draft", "type": "string" },
          { "name": "rationale", "type": "string", "optional": true }
        ],

        "bounds": {                     // the engine's own names — §9.1
          "maxSteps": 8,
          "maxTokens": 8192,
          "maxMalformedTurns": 1
        },

        "selfCritique": {               // omitted when the step has none
          "gateTool": "gate.lintPost",
          "maxRevisions": 1,
          "gateArgs": { "platform": "x" }
        },

        "isGate": false,
        "gateKind": null,               // "batch_review" | "prompt_set_review" | "fix_generation_review"
        "skillRef": "blog-agent/10-draft-post@7",

        // Present for kind "code" instead of prompt/model/bounds.
        "language": null,               // "node" | "python"
        "code": null,
        "codeTimeoutMs": null
      }
    ]
  },

  // …or, when the snapshot is too large to travel inline (§6):
  "snapshotUri": "gs://karoscmo-prep-agent-artifacts/snapshots/6d1e2f3a-….json.gz",
  "snapshotSha256": "…64 hex…"
}
```

Every field maps to a column in `config.agent_versions` or
`config.agent_version_steps` (S2). There is no field here the control plane has
to invent at resolve time, and no column there that the snapshot drops.

## 4. Both transports exist from day one

`snapshot` and `snapshotUri` are mutually exclusive and the engine must accept
**both** in the first version that accepts either.

This is the one implementation instruction in the contract, and it is here
because the alternative is a coordinated deploy under pressure. Inline is what
will actually be used (§6 measures why), so a consumer that only handles inline
will pass every test and work for months. The first agent that outgrows the
ceiling then needs the producer and the consumer changed together, at the
moment someone is trying to ship an agent. Handling `snapshotUri` while nobody
is sending one costs an afternoon; adding it later costs a release window.

`snapshotSha256` accompanies `snapshotUri` so a fetched snapshot is verifiable.
A truncated GCS read is otherwise valid JSON with fewer steps in it, and a run
that quietly skips its last stage produces a deliverable that looks finished.

## 5. Resolution: published, or pinned to a client

The resolver picks, in order:

1. `config.client_agent_config.pinned_version_id` for `(clientSlug, agentSlug)`,
   if set and the row is `enabled`. → `resolvedFrom: "client_pinned"`
2. `config.agents.published_version_id`. → `resolvedFrom: "published"`

If neither exists the dispatch **fails, before publishing**, with an error
naming the agent. It does not fall back to a draft: a draft is editable, and
the whole contract is that a running configuration is not.

`resolvedFrom` travels because "why did this client get different output from
that one" is otherwise a question requiring database archaeology at exactly the
moment someone is annoyed.

`stageModels`, which the message already carries, becomes an **input to the
resolver** rather than something the engine merges (§9.3).

## 6. Size, measured

The ticket's arithmetic is 40 × 20KB = 800KB against Pub/Sub's 10MB. Two things
that arithmetic omits: JSON structure around the prompt bodies, and base64 — a
Pub/Sub message's `data` is base64 on the wire, which is a flat ×4/3.

Measured, on a representative snapshot with realistic prose in the prompt
bodies (two tools, an output schema and a self-critique block per step):

| Case | JSON | base64 (the wire) | Headroom to 10MB |
|---|---|---|---|
| Worst real product — `reddit-agent`, 24 stages | 0.51 MB | 0.68 MB | ×14.8 |
| The ticket's 40 × 20KB | 0.84 MB | **1.12 MB** | ×8.9 |
| 40 × 40KB — double the prompt assumption | 1.64 MB | 2.19 MB | ×4.6 |
| 100 × 20KB | 2.11 MB | 2.81 MB | ×3.6 |
| All 250 stages in the system, in one message | 5.27 MB | 7.03 MB | ×1.4 |

So: comfortable, and specifically comfortable at *ten times* the largest agent
that exists. `reddit-agent` is the current worst case at 24 stages, and the 40
in the ticket is already a generous allowance.

**Threshold: 5 MB of base64.** Above it the producer writes the snapshot to
`gs://…-agent-artifacts/snapshots/{snapshotId}.json.gz` and sends
`snapshotUri`. Half the ceiling, so a snapshot that grows between the size
check and the publish cannot cross it.

**Not gzipped inline.** Gzip takes the 40 × 20KB case from 1.12 MB to 0.19 MB,
and it is still the wrong default: it makes the message opaque to everyone
reading a dead-letter queue, and it buys headroom against a limit that is
already ×8.9 away. It applies to the GCS object, where nobody is reading the
bytes by eye.

**Do not shrink what the snapshot contains** to save bytes. Every field here is
either something the engine needs to execute the step or something the ledger
needs to explain the cost. The escape hatch is the URI, not a smaller snapshot.

## 7. Invariants

### 7.1 The snapshot is resolved once per run, and reused on resume

A resumed run — a `batch_review` gate that a human answers three days later —
executes against **the snapshot it started with**, not a fresh resolution. This
is not a detail: the gate case is the longest-lived run in the system and
therefore the one most likely to see its configuration edited mid-flight, which
is the failure the whole contract exists to prevent.

So the snapshot (or its URI plus hash) is persisted with the run and re-read on
resume. S6 owns where: `agent_runs` has no column for it today, and the durable
step store is the other candidate. Whichever it is, resolving again on resume is
a bug, not an optimisation.

### 7.2 `agentSlug` must equal `productId`, and the engine checks

Two fields naming the same thing is redundancy on purpose: it means a snapshot
attached to the wrong message is caught rather than executed. The engine
compares them and refuses the run on a mismatch — a `tooling_error`, not a
content failure, because nothing about the client's request is wrong.

### 7.3 An unknown `schemaVersion` is refused, not best-effort parsed

A consumer that skips fields it does not recognise will run a snapshot from a
newer producer with, say, its `selfCritique` block silently dropped, and produce
a deliverable that passed no gate. Refuse, with the version in the message.

### 7.4 Prompt bodies travel with their `contentHash`

`contentHash` is the sha256 the control plane stored (S2 constrains the column
to 64 hex characters). It is not for the engine to validate on every run — it is
so that "the prompt that produced this deliverable" is a verifiable claim months
later, when the only surviving artifact is the snapshot and someone doubts the
version numbering.

### 7.5 Pricing travels, and the engine never looks a price up

Each step's `model.pricing` is the row that was in effect at resolve time. The
engine's `pricingForModel` currently falls back to `DEFAULT_MODEL_PRICING`
($3/$15) on a miss, silently — which bills Opus work at a third of its cost and
`gemini-1.5-flash`, the tertiary fallback model, at ten times its own. With the
price in the snapshot there is nothing to miss, and a run's cost is reproducible
from the run's own record.

Because the price is frozen with the version, a vendor price change does not
retroactively re-price completed runs. That is the correct behaviour and it is
worth saying out loud, because the intuitive alternative — always price against
the current table — makes last quarter's cost report change when a vendor
updates a number.

### 7.6 A `pinned` step has no fallback model

The engine already refuses a fallback declared alongside `pinned` rather than
ignoring it, and the S2 schema refuses it too. The snapshot carries
`fallbackModel: null` in that case; a snapshot with both is malformed.

### 7.7 The snapshot is configuration, and nothing else

Not in it, deliberately:

* **Client context documents (C1).** A projected workspace the engine reads
  through `client.getContextDoc`. It is per-run *data* that should be as fresh
  as possible, and freezing a brand-voice document into a snapshot would mean a
  correction to it not reaching a queued run — the opposite of what is wanted
  for data, and the opposite of what is wanted for configuration.
* **The run's own input (C3).** `input` stays a sibling field on the message.
  It belongs to the person who dispatched the run; the snapshot belongs to
  whoever administers the agent.
* **Media assets.** Same reason: run-scoped, not configuration.
* **Credentials of any kind.** `allowedTools[].config` carries configuration;
  a secret is named by reference in the control plane and resolved by the engine
  from its own environment. A snapshot is written to a Pub/Sub message and
  sometimes to a GCS object, and both outlive the run.

## 8. The consumer side (Tomer)

What changes in `agent-engine`:

1. `RunJobRequestSchema` gains `snapshot` and `snapshotUri` + `snapshotSha256`,
   mutually exclusive, both optional.
2. `startRunJob` builds the workflow's step configuration from the snapshot when
   one is present. `AgentStepConfig` is very close to a step already:
   `allowedTools`, `outputSchema`, `maxSteps`, `maxTokens`,
   `maxMalformedTurns`, `modelPolicy`, `skillRef`, `selfCritique` all map
   one-to-one.
3. `PromptStore` is not consulted for a step whose snapshot carries a body. The
   store stays for the paths that have no snapshot (§8.1).
4. `AgentDefinitionStore` is not consulted for a `productId` that arrives with a
   snapshot.
5. `ModelRouter` receives an already-resolved `providerModelName` + `region` and
   does not resolve an alias. `MODEL_ALIASES` stays for the direct-API paths.
6. Telemetry records `snapshotId`, `agentVersionId` and `agentVersion` on every
   step span, which is what makes the cost report joinable back to a
   configuration.

### 8.1 The no-snapshot path keeps working, and says so

A run with no snapshot resolves the way it does today. That path is needed for
local development, for `POST /api/v1/runs/start` called by hand, and for a
dynamic agent registered directly through `POST /api/agents`.

What it must not be is silent. A run that executes without a snapshot is
recorded as such — one field on the run record, `configSource:
"snapshot" | "stores"` — because the two are not equivalent and a run that fell
back to the stores has no answer to "which configuration produced this".

## 9. Three findings that shape the contract

### 9.1 "retry, timeout" does not exist in the engine's step config

The ticket asks the snapshot to carry "allowedTools, retry, timeout". The engine
has neither of the last two at step level for an AI step. What it has:

| Ticket says | Engine actually has | Scope |
|---|---|---|
| retry | `maxMalformedTurns` (default 1) — a malformed turn is re-prompted with its own validation error | per step |
| | `selfCritique.maxRevisions` (default 1) — the gate loop | per step |
| timeout | `agentStepTimeoutMs` (default 10 min) — `WorkflowRuntime`, applied to **every** `step.agent` call in the run | **per run** |
| | `timeoutMs` — the dynamic sandbox's budget for a code stage | per step, code only |

There is no per-step attempt count and no per-step backoff. Inventing them for
the snapshot would mean shipping configuration no consumer can read, which is
how a wrong implementation grows to match a schema. So the snapshot uses the
engine's names, and the S2 schema was corrected to match before this document
was written (`max_malformed_turns`, `max_tokens`, `self_critique_*`,
`code_timeout_ms` scoped to code steps by a CHECK, and
`agent_versions.agent_step_timeout_ms` for the per-run one).

If a per-step retry with backoff is actually wanted, it is a change to
`step-agent.ts` and a ticket of its own — not a field in this contract.

### 9.2 `vendor` means two different things in the two repositories

`agent-middleware`'s `ModelVendor` is `anthropic | google | meta | other` — who
makes the model. `agent-engine`'s `ModelVendorSchema` is
`anthropic | gemini | model-garden | openai-compatible` — how this deployment
reaches it. Llama on Model Garden is `vendor: meta, route: model-garden`.

The snapshot carries both, under two names, because collapsing them makes that
model inexpressible and whoever hits it will "fix" it by picking whichever axis
their own file cared about.

### 9.3 `stageModels` and the snapshot both decide a step's model

The message already carries `stageModels` — a per-step model override for one
run, authored in Studio. If the snapshot also resolves each step's model, two
fields decide the same thing and the engine has to have a merge rule.

**It should not have one.** `stageModels` becomes an input to the *resolver*:
the control plane applies the override, then freezes. The snapshot is then the
single answer to "what model did this step use", and there is exactly one place
that decision is made.

That means `stageModels` stops being read by the engine for snapshot-carrying
runs, which is a coordinated change and the reason this is a finding rather
than a footnote. Until it lands, an engine that receives both must prefer the
snapshot and record that it ignored `stageModels`, rather than applying an
override on top of a frozen configuration.

## 10. Acceptance criteria

1. A run dispatched with a snapshot executes with zero reads of `PromptStore`,
   `AgentDefinitionStore` or Firestore configuration. Asserted with a spy, not
   by inspection.
2. Editing a prompt, a model or a step between dispatch and execution does not
   change the run's behaviour. A test that publishes, edits, then executes.
3. A gate resumed after an edit executes against its original snapshot (§7.1).
4. A snapshot whose `agentSlug` differs from the message's `productId` is
   refused as a `tooling_error`.
5. An unknown `schemaVersion` is refused with the version in the message.
6. A `snapshotUri` whose object does not match `snapshotSha256` is refused.
7. The engine handles `snapshotUri` in the same release that handles `snapshot`,
   proven by a test that sends one (§4).
8. Cost for a completed run computes from `model.pricing` in the snapshot, with
   no call to `pricingForModel`.
9. A run with no snapshot still works, and records `configSource: "stores"`.
10. The largest real agent (`reddit-agent`, 24 stages) travels inline. A
    synthetic 250-stage agent travels by URI. Both execute.

## 11. What this contract does not decide

* **Where the snapshot is persisted for resume.** §7.1 requires it; S6 chooses
  between a column on `agent_runs` and the durable step store.
* **Whether the engine keeps its prompt stores long-term.** §8.1 keeps them as a
  named fallback. Retiring them is a separate decision, after the no-snapshot
  path is measured at zero.
* **The retention of GCS snapshot objects.** They are the evidence behind "which
  configuration produced this deliverable", so the answer is probably "as long
  as the deliverable", but that is a policy call and a cost one.
