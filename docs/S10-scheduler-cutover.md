# S10 — unifying the two scheduling systems: the cutover plan

SCRUM-223. This document is the coordination the ticket says it requires with
Tomer on the portal side. The middleware half is in this branch; the portal
half is what this plan asks for.

## The problem, in one sentence

karosCMO has two scheduling systems, and one of them — `/api/scheduler`,
draining `scheduledRuns` — passes `charge: null` unconditionally, so every one
of its fires is free to the client and absent from the credit ledger.

The other — `/api/run-scheduled`, draining `plannedScheduledRuns` — hands the
submit core an explicit `bill` decision per row. Two crons, two collections,
two cadence models, one money bug.

## What the middleware now provides

`config.schedules` (S2) already had the merged row shape. This branch adds
what uses it:

| Piece | Where | What it settles |
|---|---|---|
| `ScheduleService` | `app/services/schedules.py` | Create/list/pause/resume; `claim_due` and `settle`; the cadence math |
| Routes | `app/api/routes/schedules.py` | `/clients/{slug}/schedules` (editor) and `/schedules/claim`, `/schedules/{id}/settle`, `/schedules/in-flight` (editor) |
| Importer | `app/services/schedule_import.py`, `scripts/import_schedules.py` | Both collections → one table, one-way, idempotent |
| Tests | `tests/test_schedules.py` | 22, against a real PostgreSQL 16 |

Three properties the portal's two systems do not have:

1. **A fire cannot be claimed without a stated billing decision coming back
   with it.** `bill_client_credits` is `NOT NULL` with no default; every row
   `POST /schedules/claim` returns carries it. There is no code path that
   yields a fire and not the answer.
2. **The claim is `SELECT … FOR UPDATE SKIP LOCKED`.** Two ticks at the same
   instant get disjoint sets. The cursor advances and `fire_in_flight_since`
   is stamped in the statement that returns the row. `test_concurrent_claims_get_disjoint_sets`
   observes it.
3. **A vanished fire recovers itself.** A row still in flight after
   `IN_FLIGHT_GRACE` (30 min) is re-claimed on the next tick with
   `last_error` naming the claim that never settled. The portal's
   `fireInFlightSince` today is a marker nobody acts on.

## What the middleware deliberately does NOT do yet

Submit the job, or charge for it. Billing lives in the portal's submit core
and credit ledger (`submitCustomAgentRun`, `credit-reporting.ts`). Moving that
is a different ticket. So the protocol is: **the middleware owns the schedule
and the claim; the portal executes the fire and reports back.**

## The cutover, step by step

### Step 0 — prerequisites (Shlomi)

- `CONFIG_DB_DSN` set on prep (S1 / SCRUM-216 console questions).
- Migrations applied through `0006`.
- `scripts/import_registries.py` has run, so `config.agents` carries the
  `customAgents` ids as `source_id` — the importer maps `agentId` /
  `customAgentId` through it.

### Step 1 — import, dry run first (Shlomi)

```
python -m scripts.import_schedules --env prep --dry-run
```

Read the refusals. Each is a question for a person, not a bug in the script:

| Refusal | Who answers | The question |
|---|---|---|
| `billClientCredits is absent` on a `plannedScheduledRuns` row | Daniel / Tomer | Does this schedule bill the client? The portal falls back to an actor test; the import will not read that as a decision. Set the field on the document, re-run. |
| `cadence 'once'` | Tomer | A planned single run is a queued job, not a schedule. Does it stay in `plannedScheduledRuns` under the old cron until it fires, or get its own home? |
| `timeZone is absent` | Tomer | Which zone did this client mean? `--assume-time-zone Asia/Jerusalem` imports them all under one stated zone if that is the answer for every row. |
| `has no clients document with an agentsRepoSlug` | Shlomi | The client has no workspace slug yet; the schedule cannot be attributed. |

Then without `--dry-run`. Run it twice; the second run must report only
`already_present`.

### Step 2 — the portal's two crons read from here (Tomer)

Both `/api/scheduler` and `/api/run-scheduled` change from *read my collection,
claim by compare-and-set, submit, write back* to:

```
POST {middleware}/schedules/claim            { "limit": 25 }
  → { claim_id, fires: [ { id, client_slug, agent_slug, prompt, outputs_per_run,
                            bill_client_credits, fired_for, ... } ] }

for each fire:
  submitCustomAgentRun({ ..., charge: fire.bill_client_credits ? <charge> : null })
  POST {middleware}/schedules/{fire.id}/settle
       { claim_id, job_id }            on success
       { claim_id, error }             on refusal (stored on the row, cursor already advanced)
       { claim_id, error, disable: true }   when the agent or client is gone
```

Two things to keep from the current crons: the pre-fire slot check in
`/api/run-scheduled` (a filled day must not produce a second post), and the
`notifyScheduleFireFailure` alert. Both run between claim and settle,
unchanged.

What goes away: `claimScheduledRun` / `claimPlannedScheduledRun`
(compare-and-set on `nextRunAt`), `computeNextRunAt` on the portal side (the
middleware advances the cursor at claim), and the `charge: null` line — the
value now comes off the row.

Auth: the crons already carry `CRON_SECRET`; the middleware routes want the
service identity the portal already uses for `POST /agents/{id}/jobs`, bound to
the editor role (S3) — the same floor `/jobs` has, because a claim can spend
a client's credits.

### Step 3 — one cron, then none of the old reads (Tomer)

Once both routes read from the middleware, they are the same route. Merge them
into one tick. `scheduledRuns` and `plannedScheduledRuns` become write-only
(new schedules created from the settings card go to
`POST /clients/{slug}/schedules`, which requires `bill_client_credits` and so
cannot recreate the bug), then read-nowhere, then deletable — `docs/legacy-firestore-inventory.md`
item 2.

### Step 4 — the money decision (Daniel)

Every row imported from `scheduledRuns` carries
`billing_intent_source = 'inferred_at_import'` and `bill_client_credits = false`.
That is a description of today, made visible. Whether those schedules start
billing is a product decision; when it is made, it is one `UPDATE` per
client, and the intent source flips to `explicit`.

## What the portal's tests should pin after Step 2

- A claimed fire with `bill_client_credits: true` reaches the submit core with
  a non-null charge; with `false`, with `charge: null`. The value comes from
  the fire, not from which route is running.
- A settle carrying a stale `claim_id` is refused (409) and the cron treats it
  as "someone else owns this fire now" — no submit.
- The cron never computes `nextRunAt`.

## Open questions this branch does not answer

1. Should the middleware eventually execute the fire itself (calling the
   engine directly, charging through a ledger it owns)? That is the natural
   end state and would retire the portal cron entirely, but it needs the
   credit ledger to move first.
2. `once` cadence — see the refusal table.
3. Whether `IN_FLIGHT_GRACE` at 30 minutes is right for the slowest legitimate
   submit today. It is a constant in one place.
