# C7 · Run context — what a run reads from its workspace, and what it writes back

**Status:** draft for Tomer's sign-off · SCRUM-458 · correctable until the first branch that depends on it merges
**Reads (engine):** Shlomi — `client.getLearningContext`, `readLearningContext` (SCRUM-459/466)
**Writes back (engine):** Shlomi — `ledger.writeRunState`, `writeRunState` (SCRUM-460)
**Projects (before dispatch):** agent-middleware for the Postgres-held stores (SCRUM-461); the portal for its own documents (SCRUM-468, C1 as it stands)
**Collects (after the run):** agent-middleware `POST /runs/{run_id}/collect` (SCRUM-461); the portal's reconcile calls it (SCRUM-469)
**Extends:** C1 (context documents). Does not replace it.
**Decision it rests on:** O09 — the learning tables live in Postgres (`config` schema, agent-middleware); the engine never reads Postgres or Firestore (SCRUM-465)

---

## 1. The principle

Build plan items A1 and A2 (`04 Build plan`, `02 Learning Loop §3.3`): every run receives
everything the platform has learned about the client, **live at run time**, and hands back
what it learned, in one normalised record. The engine reads and writes **files in its own
workspace**, exactly as it does today for C1's documents. Who projected a file, and from
which database, is not the run's business; a projected file carries provenance so the
question can be answered afterwards.

Everything below is optional from the run's point of view. A missing or empty file degrades
to "not present" inside the run — never a throw, never a held run. That is the same
invariant `client.getContextDoc` / `readContextDoc` already hold, and it is what lets the
portal, the middleware and the engine land in the same week without a merge fight: a run
on a tree that has only some of the writers behaves exactly as it does today.

## 2. What a run reads

All paths are under `gs://<bucket>/clients/<slug>/` (`WorkspaceStore` segments in brackets).

| File | Segments | Written by | Holds |
|---|---|---|---|
| `context/<docType>.json` | C1, unchanged | portal / middleware `ClientContextProjector` | the nine C1 documents |
| `client/competitors.json` | C1, unchanged | same | competitor list |
| `context/learning/<platform>/platform-state.json` | `["context","learning",platform,"platform-state"]` | middleware projector, from `config.platform_state` | the introduction doc — §2.1 |
| `context/learning/<platform>/subject-window.json` | `[…, "subject-window"]` | middleware, from `config.subject_rows` (anti-repeat window) | §2.2 |
| `context/learning/<platform>/feedback.json` | `[…, "feedback"]` | middleware, from `config.client_feedback_log` | §2.3 |
| `context/learning/preferences.json` | `["context","learning","preferences"]` | middleware, from `config.client_preferences` | §2.4 |
| `context/learning/<platform>/what-works.json` | `[…, "what-works"]` | middleware, from the what-works summary (absent until ingestion exists) | §2.5 |
| `context/learning/<platform>/strategy-map.json` | `[…, "strategy-map"]` | middleware, from `config.strategy_map`; first built by the engine (C1 item, SCRUM-464) | §2.6 |
| `context/learning/<platform>/craft.json` | `[…, "craft"]` | middleware, from the craft store (L1 + L2 + L3 merged) | §2.7 |
| run input `slotStage` | Pub/Sub message, `RichRunInputSchema` | portal sequencing, for calendar runs only | `attention` \| `expertise` \| `decide` |

`<platform>` is the engine's platform key: `x`, `linkedin`, `reddit`, `instagram`, `tiktok`.

### 2.0 The envelope every projected file shares

```jsonc
{
  "kind": "platform-state",            // = the file's own name
  "platform": "x",                     // absent on preferences.json
  "data": { … },                       // the payload, §2.1–§2.7
  "source": {
    "projectedAt": "2026-09-16T09:00:00Z",
    "projectedBy": "middleware-dispatch | middleware-backfill | engine-run",
    "contentHash": "sha256:…",         // of the serialised `data` — the idempotency key
    "rows": 12                          // optional: how many store rows the file was built from
  }
}
```

Same shape as C1's `{markdown, source}` with `data` in place of `markdown`. The reader returns
`data` and keeps `source` for the readiness line; it never recomputes `contentHash`.

### 2.1 `platform-state` — the introduction doc (02 §3.2)

```jsonc
{
  "account": { "handle": "@acmehq", "url": "…" },
  "followers": 4210, "postsTotal": 380, "postsByUs": 14,
  "topPosts": [{ "url": "…", "why": "a number in the first line", "metric": "replies/impression 4.1%" }],
  "whatWorks": ["…"], "options": ["…"],
  "voiceNotes": ["short declaratives; never a rhetorical question opener"],
  "lastUpdated": "2026-09-15T20:11:00Z"
}
```

### 2.2 `subject-window` — the rows the anti-repetition rule reads

```jsonc
{
  "windowDays": 30,
  "rows": [{
    "id": "uuid", "runId": "pubsub-…", "subject": "…", "angle": "…", "type": "knowledge",
    "stage": "expertise", "goal": "show expertise", "status": "posted",
    "draftedAt": "…", "postedAt": "…"
  }]
}
```

A subject in this window is not proposed again on this platform. The window is a
per-platform setting on the middleware side (`anti_repeat_days`, default 30).

### 2.3 `feedback` — what the client did with recent drafts on this account

```jsonc
{ "rows": [{ "runId": "…", "account": "@acmehq", "action": "posted_with_edits",
             "reason": null, "originalText": "…", "finalText": "…", "at": "…" }] }
```

`action ∈ posted | posted_with_edits | skipped | change_requested | note`.

### 2.4 `preferences` — derived, client-wide

```jsonc
{ "neverTopics": ["pricing"], "likes": [{ "note": "…", "postRef": "…" }],
  "voiceNotes": [{ "lesson": "…", "fromRunId": "…" }], "standingInstructions": ["…"],
  "derivedAt": "…", "derivedFromCount": 23 }
```

Voice notes come only from edits (Craft 11 §3), never from likes alone.

### 2.5 `what-works` — outliers and the rules derived from them

```jsonc
{ "outliers": [{ "postRef": "…", "trait": "number in line 1", "lift": 3.1 }],
  "rules": [{ "id": "L3-x-014", "rule": "…", "sampleSize": 12, "since": "…" }],
  "regeneratedAt": "…" }
```

### 2.6 `strategy-map` — the topic pool with goals (C1)

```jsonc
{ "platform": "x", "builtAt": "…", "source": "setup-run | first-run | manual",
  "audience": [{ "role": "Head of Ops", "problems": ["…"] }],
  "rows": [{ "id": "sm-x-007", "problem": "…", "stage": "attention", "idea": "…",
             "type": "news-reaction", "evidence": "…", "status": "open" }],
  "defaultMix": { "attention": 3, "expertise": 2, "decide": 1 } }
```

### 2.7 `craft` — the three layers, merged, with precedence resolved

```jsonc
{ "platform": "x",
  "rules": [{ "id": "L1-x-003", "layer": "L1", "kind": "hard", "rule": "…", "why": "…", "metric": "…" },
            { "id": "L2-saas-x-002", "layer": "L2", "kind": "default", "rule": "…", "metric": "…" },
            { "id": "L3-acme-x-001", "layer": "L3", "kind": "default", "rule": "…", "metric": "…", "sampleSize": 12 }],
  "overrides": [{ "winner": "L3-acme-x-001", "loser": "L1-x-011" }] }
```

Precedence is resolved by the projector, not by the run: L3 beats L2 beats L1 defaults;
**L1 hard rules always win** and can never appear as a `loser`. The run receives rules as
instructions (D41), not as a checklist, and reports the ids it applied (§3).

## 3. What a run writes back

| File | Segments | Written by | Collected by |
|---|---|---|---|
| `state/runs/<runId>.json` | `["state","runs",runId]` | `ledger.writeRunState`, once per run, at the commit step | middleware `collect` |
| `state/<platform>/platform-state.json` | `["state",platform,"platform-state"]` | same tool, upserted (first run builds it) | middleware `collect` |
| `state/<platform>/strategy-map.json` | `["state",platform,"strategy-map"]` | setup / first runs (SCRUM-464) | middleware `collect` |

### 3.1 The run state record

```jsonc
{
  "schemaVersion": 1,
  "runId": "pubsub-…", "clientSlug": "acme", "productId": "x-agent", "platform": "x",
  "writtenAt": "…",
  "deliverable": {
    "kind": "x-post",
    "goal": "attention | expertise | decide",
    "audience": "who it is for — the problem it speaks to",
    "whyNow": "news | trend | request | a post of this kind that performed — in one line",
    "type": "knowledge",                    // the platform's own type vocabulary (X: lane; LinkedIn: post type; Reddit: reply)
    "sources": ["https://…"]
  },
  "subjectRow": {
    "subject": "…", "angle": "…", "type": "knowledge", "stage": "expertise", "goal": "…",
    "status": "drafted", "assetKind": "x-post", "strategyRowId": "sm-x-007 | null"
  },
  "platformStateDelta": { "postsByUs": 1, "topics": ["…"], "lastUpdated": "…" },
  "voiceNotes": [{ "lesson": "…", "fromRevision": 1 }],
  "rulesApplied": ["L1-x-003", "L3-acme-x-001"],
  "readiness": { "present": ["platform-state", "subject-window"], "absent": ["what-works", "craft"] }
}
```

`goal` uses the same three words as `slotStage`. `readiness` is the A1 acceptance line: which
of the projected files the run actually found.

## 4. Invariants

1. **Every file is optional for the reader.** Missing → not present; present but malformed →
   not present, logged once with the path. A run with nothing projected behaves exactly as a
   run did before this contract.
2. **The writer is idempotent on `runId`.** Re-running a resumed run rewrites the same record;
   `platform-state` upserts merge by field and never lose a value the projector wrote.
3. **The engine never reads Postgres or Firestore for any of this.** Projection is the
   middleware's and the portal's job; collection is the middleware's.
4. **Provenance travels.** Every projected file carries `source`; the record carries
   `readiness`. Freshness is measured, not assumed.
5. **The vocabulary is shared, not translated.** `stage`/`goal` = `FUNNEL_STAGES` in
   `@agent-engine/core`; `action` = the five feedback actions above; `kind` = C5's deliverable
   kinds. A middleware column and an engine field with the same meaning have the same name.

## 5. Acceptance

- An X run on prep with no `context/learning/` files completes unchanged and its record's
  `readiness.absent` lists all seven.
- With the files projected, the draft input carries each of them (unit test per key) and the
  record's `readiness.present` lists them.
- After the run, `state/runs/<runId>.json` and `state/x/platform-state.json` exist with every
  field in §3.1, and `collect` ingests them with no mapping layer.
