# C1 · Client context documents in the engine workspace

| | |
|---|---|
| **Status** | Draft for approval by Shlomi and Tomer |
| **Jira** | SCRUM-209 |
| **Seam** | Shlomi writes (`agent-middleware`) → Tomer reads (`agent-engine`, `karosCMO`) |
| **Producer** | `scripts/seed_client_context.py` (S-A14 / SCRUM-228) and the re-seed endpoint (S-A15 / SCRUM-229) |
| **Consumer** | `client.getContextDoc` in the engine (T-A8) and the portal-side projector (T-B13) |
| **Blocks** | T-A8, T-A9, T-A10, T-A11, T-B13, S-A14, S-A15 — and every cutover downstream of them |
| **Verified against** | `agent-middleware` @ `9910dc0` · `agent-engine` @ `89bb8c4` · `karosCMO` @ `fe9b5f2` |

Amendable until the first branch that depends on it merges. After that, a change
needs both sides updated in the same window.

---

## 1. The principle

> The agent reads a **projected copy** of the client's documents, never Firestore
> directly. The projection carries full provenance, so it is always answerable
> which document version reached the model, when, and by what mechanism.

Two things follow from that sentence and neither is decoration. Because the
agent reads a copy, the copy can be stale — so provenance has to be rich enough
for the readiness report to *measure* the staleness rather than assert
freshness. And because the projection is a copy of something a human wrote, the
writer is a courier, not an author: a document that does not exist in Firestore
does not exist in the workspace.

## 2. What is projected in v1

`ContextDocType` in `karosCMO/src/lib/types.ts` has twelve members. Nine are
projected:

| docType | v1 | Why |
|---|---|---|
| `brand-voice` | ✔ | |
| `market-strategy` | ✔ | |
| `competitor-analysis` | ✔ | |
| `product-information` | ✔ | |
| `branding-guidelines` | ✔ | |
| `target-audience` | ✔ | |
| `x-agent-profile` | ✔ | Complements `strategy/x-agent`; does not replace it |
| `linkedin-agent-profile` | ✔ | Type exists, only `x` is wired in the portal today |
| `reddit-agent-profile` | ✔ | Same |
| `meeting-notes` | ✘ | Noisy. Added if a specific agent justifies it |
| `client-guidelines` | ✘ | `internal-only` tier — see §4.2 |
| `action-plan` | ✘ | `internal-only` tier — see §4.2 |

Plus `clientCompetitors` → `clients/<slug>/client/competitors.json`, which is
not a context doc at all but belongs in the same projection pass: it is the one
path `client.listCompetitors` already reads
(`SEGMENTS = ["client", "competitors"]`) and nothing writes it today, so the
tool returns `not_available` for every client in every environment.

The three agent-profile docs are additive. `client.getStrategy` keeps reading
`strategy/<agent>.json`, which is a *charter* — what the account is for, what it
must never post. The agent-profile doc is the identity narrative the portal
maintains. An agent that wants both reads both; nothing is migrated.

## 3. Path and shape

```
gs://<bucket>/clients/<slug>/context/<docType>.json
```

Buckets are the existing ones: `karoscmo-prep-agent-artifacts` and
`karoscmo-prod-agent-artifacts`. The engine's workspace store appends `.json`
and stores one record per key, so the reader addresses this as segments
`["context", docType]` — the same mechanism `client.getStrategy` uses for
`["strategy", agent]`.

```jsonc
{
  "docType": "brand-voice",
  "markdown": "...",                     // the full content. This is what reaches the model.
  "source": {                            // provenance — mandatory, never omitted
    "firestoreDocId": "…",               // clientContextDocs document id
    "docVersion": 7,                     // ClientContextDoc.version at projection time
    "tier": "internal",                  // always "internal" in v1 — see §4.2
    "projectedAt": "2026-08-25T09:00:00Z",
    "projectedBy": "portal-save | seed-cli | backfill",
    "contentHash": "sha256:…"            // of markdown alone — see §4.3
  }
}
```

Three notes on the shape, each of which is a decision rather than a formatting
preference.

**`markdown`, not `content`.** The Firestore field is `ClientContextDoc.content`;
the workspace envelope calls it `markdown`. That is deliberate: it matches
`StrategyDocument` in `packages/tools/karos-client/src/get-strategy.ts`, which is
the precedent this shape follows so the engine does not grow a third envelope
for prose. The rename happens once, in the projector.

**`source` is mandatory here, and optional in `StrategyDocument`.** `getStrategy`
declares `source?: Record<string, unknown>` — "free-form provenance the
migration records". This contract tightens that: a projected context document
without complete provenance is invalid, because the whole freshness mechanism in
§5 reads those fields. A reader may treat a record missing `source` as
`not_available`; it must never treat it as fresh.

**The envelope carries no `tier` variants.** One file per docType per client. See
§4.2 for why that is safe.

## 4. Invariants

### 4.1 The writer never synthesizes

A document absent from Firestore is not written to the workspace. The seeder's
existing refusals stay exactly as they are — it declines to invent
`forbiddenTerms`, `xHandle`, `targetSubreddits` and `instagramStyleConfig` — and
the test written into `seed_client_context.py` is the one this contract adopts
for any future field:

> *If this value is wrong, does the agent take an action toward an external
> party that nobody verified, or does it merely produce something mediocre that
> a reviewer will reject? Only the second belongs to the script.*

Context documents are squarely in the second category, which is why projecting
them is safe where synthesising a channel identity is not.

### 4.2 The projector reads the `internal` tier, and never falls back

`clientContextDocs` is keyed by **(clientId, docType, tier)**, not by docType.
`getClientContextDocByTier` exists precisely because a client-facing document
and its internal twin share a `docType`, and an earlier unordered `.limit(1)`
meant callers silently drew whichever row Firestore returned first.

So: the projector names `tier: "internal"` explicitly and reads nothing else.

* It does **not** fall back to `client`. That tier is a condensed ~50%
  derivative; an agent grounded on it would produce work that looks configured
  and is thinner than the analyst's document. Falling back would make that
  failure invisible, which is the same shape as the open-failure problem T-A10
  exists to fix.
* It does **not** read `internal-only`. That tier is the never-published one,
  and it is why `client-guidelines` and `action-plan` are out of v1 — not
  because they are uninteresting but because publishing them needs a product
  decision, not a projector flag.

A docType that exists only at the `client` tier is treated as **absent**. That
is a reportable gap, not a silent downgrade.

### 4.3 Idempotent by `contentHash`, which is hashed over `markdown` alone

Re-projecting identical content is a no-op: same bytes in, `projectedAt`
unchanged.

This one needs stating because the existing seeder does it differently and the
difference is load-bearing. `seed_client_context.py` today compares
`_sha(blob.download_as_text()) == _sha(body)` — a hash of the **entire
serialized record**. That works only while the record contains nothing that
changes per run. The moment the envelope carries `projectedAt`, a whole-body
hash never matches, every projection counts as an update, and `projectedAt`
churns on every run — which destroys the very field the freshness report reads.

So the comparison is: read the existing object, compare its
`source.contentHash` against `sha256(markdown)` of the candidate. Equal ⇒ skip
the write entirely. Not equal ⇒ write the whole new envelope.

### 4.4 The reader returns `not_available` twice over

Missing file ⇒ `not_available`. Present file with empty or whitespace-only
`markdown` ⇒ `not_available`. Never a throw, never `{}`.

This mirrors `client.getStrategy` exactly, including the reason recorded in its
source: *"An empty document is worse than a missing one: it would silently hand
the model no charter while looking configured."* The same sentence applies here
word for word.

### 4.5 Projection is best-effort from the portal side

A failed projection does not fail a document save in the portal. It is logged as
structured output and it surfaces in the readiness report. A client's document
being un-projected is a visible, measurable state — never a save that appears to
have worked and did not.

### 4.6 The reader does not know who wrote

One tool serves all three producers: portal-on-save, CLI seeding, and backfill.
`projectedBy` records which one, for the audit trail. Nothing in the reader
branches on it.

## 5. Freshness

`report_client_readiness.py` gains a per-document check: the projected
`source.docVersion` against the current `ClientContextDoc.version` in Firestore.

* projected version == current ⇒ fresh
* projected version < current ⇒ **stale**, printed with both numbers
* no projected file at all ⇒ **absent**, printed alongside the existing gaps

`ClientContextDoc.version` is documented as monotonically increasing and bumped
on every write, which is what makes this comparison meaningful rather than a
timestamp race.

There is no synchronous-sync requirement in v1. The projector-on-save (T-B13) is
what actually narrows the window; this report is what makes the width of the
window a number instead of an assumption.

## 6. Acceptance criteria

1. One client projected end to end;
   `client.getContextDoc({ docType: "brand-voice" })` returns the full markdown
   with complete provenance.
2. Missing document ⇒ `not_available`. Present-but-empty `markdown` ⇒
   `not_available`. Neither throws, and neither returns `{}`.
3. Projecting identical content twice leaves `projectedAt` unchanged (no-op by
   `contentHash`).
4. A docType present only at the `client` tier is reported absent, not
   projected.
5. The readiness report shows fresh / stale / absent per document per client,
   with the two version numbers on every stale line.
6. `clients/<slug>/client/competitors.json` exists for a projected client and
   `client.listCompetitors` returns rows rather than `not_available`.

## 7. Open question for Tomer

The engine-side tool name is `client.getContextDoc` throughout this document,
per SCRUM-209. If T-A8 lands it under a different name, this file changes with
it in the same window — the name is part of the seam, not an implementation
detail on one side of it.
