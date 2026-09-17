# What the middleware owes an agent

**Status:** the standard · owner Shlomi · companion to `agent-engine/docs/AGENT-ARCHITECTURE.md`
**Rests on:** C7 (`docs/contracts/C7-run-context.md`), O09 (the learning stores are Postgres,
`config` schema), migration `0007_learning_loop.sql`

The engine never reads a database. That single rule is what this service exists to make
true: everything an agent knows about a client arrives as a **file projected into the run's
workspace**, and everything it learns comes back as a **file this service collects**. If a
run had to query Postgres, the engine would need credentials, a connection pool, and a
reason to care which database a fact lives in — and every one of those is a thing that
breaks at 3am on someone else's deploy.

Read `agent-engine/docs/AGENT-ARCHITECTURE.md` first. This file is the other half.

---

## 1. The two moments

```
dispatch ──► project ──► [ run ] ──► collect ──► stores
            (before)                 (after)
```

**Project**, in `LearningService.project`, immediately before a run is dispatched. It writes
the seven C7 §2 files for one client × platform into the workspace. It is **best-effort**:
it never raises for data, and a client with nothing in the stores gets no files and a run
that behaves exactly as it did before the loop existed.

**Collect**, `POST /runs/{run_id}/collect`, after the run finishes. It reads
`state/runs/<runId>.json`, `state/<platform>/platform-state.json` and
`state/<platform>/strategy-map.json` and folds them into the stores. It is idempotent on
`runId`; calling it twice reports `reprojected: 0` the second time, because the upserts carry
`where … is distinct from …` guards.

Nothing in between. There is no third moment, and an agent that wants one is asking for the
engine to read a database.

### 1.1 `run_id` is two ids, and collect accepts both

This service mints a run id (a uuid, or one the portal supplied) and keys `agent_runs` on it.
agent-engine derives its OWN from Pub/Sub's message id — `pubsub-<messageId>`, see its
`queue-consumer.ts` — and that is the id in every path it writes, `state/runs/<runId>.json`
included. The portal never holds ours: dispatch returns it and the portal drops it.

So collect read `state/runs/<our uuid>.json`, found nothing, every time, for every run, and
answered `collected: false — the run wrote no state file` about runs that had written one.
`LearningService._resolve_run` bridges the two: ours resolves directly, `pubsub-…` resolves
through `RunService.find_by_pubsub_message_id`. **What is stored is always the engine's**,
because it is the id the state files, the deliverables and the portal all agree on.

A fixture that writes the state file under our id is a fixture describing a run that cannot
occur. That is exactly how this went unnoticed.

---

## 2. The stores

Migration `0007_learning_loop.sql`, all in schema `config`:

| Table | Holds | Feeds |
|---|---|---|
| `subject_rows` | one row per thing an agent drafted | `subject-window.json`, anti-repetition, reporting |
| `client_feedback_log` | what the client did with a draft — **append-only, by trigger** | `feedback.json`, and the derived preferences |
| `client_preferences` | never-topics, likes, voice notes, standing instructions | `preferences.json` |
| `platform_state` | the introduction doc per client × platform | `platform-state.json` |
| `strategy_maps` + `strategy_map_rows` | the topic pool with a stage on every row | `strategy-map.json` |
| `craft_rules` | L1 / L2 / L3, with precedence resolved on the way out | `craft.json` |
| `run_state_records` | what `collect` ingested, verbatim | audit |
| `learning_settings` | `antiRepeatDays`, sector, row limits | all of the above |

`client_feedback_log` is append-only because a verdict is history. The guard is a trigger,
not a code path, so it holds for every role including the owner. Granting a privilege is not
the same as being able to use it, and that is the point.

### 2.1 Precedence is resolved here, not in the run

`craft.json` arrives at the engine already merged: L3 beats L2 beats L1 defaults, and **L1
hard rules always win and can never appear as a loser**. The run receives rules as
instructions (D41) and reports which ids it applied. Do not push the merge into the engine;
three agents would then implement it three ways.

---

## 3. Which platform a product belongs to

`platform_for_product` in `app/services/learning_store.py` decides whether a dispatch
projects anything at all. Get this wrong and the agent silently runs with no learning
context — the failure mode is a run that works, which is the worst kind.

The mapping is **explicit**, not derived from the product id's spelling. Several products
share one platform key, because a client has one account and one subject history per
platform, not one per agent we happen to sell:

- the three TikTok agents — clipping, editing, content design — all map to `tiktok`
- `instagram-agent` maps to `instagram`

A product with no platform — the SEO audit, the landing builder, the orchestrator — maps to
`None`, and the caller skips it rather than projecting under a made-up key.

### 3.1 Adding a platform

A platform key is a bigger change than a product id, and it is the only one that touches SQL:

1. `PLATFORMS` in `app/services/learning_store.py`.
2. The `CHECK` constraints in a **new** migration — `0007` is applied; never edit an applied
   file. The constraints and `PLATFORMS` must agree, and nothing enforces that but you.
3. The engine's `LearningPlatform`, `LEARNING_PLATFORMS`, `StrategyMapPlatform` and the
   `ledger.writeStrategyMap` enum.
4. The portal's own platform vocabulary.

### 3.2 Adding a product to an existing platform

One entry in the product→platform map, and nothing else. No migration, no enum, no
projection change. That is the payoff for keying the stores on the platform rather than on
the product.

---

## 4. Vocabulary is shared, never translated

A middleware column and an engine field with the same meaning have the same name:

| Meaning | The one spelling |
|---|---|
| funnel stage / goal | `attention`, `expertise`, `decide` |
| what the client did | `posted`, `posted_with_edits`, `skipped`, `change_requested`, `note` |
| where a subject row stands | `drafted`, `approved`, `posted`, `skipped`, `change_requested` |
| what was produced | C5's deliverable kinds |

`STATUS_FOR_ACTION` is the one deliberate join between two of those lists: a review action
moves the subject row it was about, which is what keeps B1 and B2 from drifting apart.

The moment one side translates, every report that joins across platforms stops working, and
it stops working quietly.

---

## 5. Migrations

Plain `.sql` in `migrations/`, applied in filename order, recorded in
`config.schema_migrations`. There is no migration framework and adding one before a second
consumer exists would be choosing a tool for a problem that has not arrived.

**Apply them with `gcloud sql import sql`, not Cloud SQL Studio:**

```bash
gcloud sql import sql karos-config-prep gs://<bucket>/<path>/00NN_name.sql \
  --project karoscmo-prep --database=karos-prep --user=postgres --quiet
```

The Cloud SQL service agent executes the file as the user named in `--user`; authorization is
IAM on the bucket object, and no database password is involved. Studio needs a Google
sign-in and the `postgres` password, which makes every migration a task only a person can
do — for months that was the single thing blocking the learning loop from working on prep.

`--user=postgres` is not optional: `ALTER DEFAULT PRIVILEGES` from `0001` only covers tables
created by that role, so a migration applied as anyone else leaves the runtime service
account unable to read its own new tables.

**Verify with a block that raises**, not with a `SELECT`. An import prints no query output, so
a `SELECT` proves nothing; a `do $$ … raise exception … $$` block fails the import itself,
and a green import is the passing result. Prove the check is not vacuous once, with a file
whose only statement is `raise exception`.

Two roles, always: the migration role needs `CREATE` on the database, the runtime role never
needs DDL. A runtime role that can `DROP TABLE` is one injection away from being the only
backup that mattered.

---

## 6. Tests

`tests/test_learning_postgres.py` runs against a real Postgres (`pgserver`), not a mock,
because everything worth testing here is a constraint, a trigger or an upsert guard — and a
mock of a trigger is a test of the mock. Anything new in `config` gets a test in the same
file, and the append-only guards get a test that tries the write and expects the refusal.

`tests/test_security.py` holds the count of protected routers and the list of admin-only
routes. A new route under `/learning` that is not in that list is a new route anyone can
call.
