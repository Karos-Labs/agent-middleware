# Migrations — the configuration plane

SQL for the Postgres side of the control plane (SCRUM-217 / S2). Plain `.sql`
applied with `psql`: there is no migration framework here yet, and adding one
before there is a second consumer would be choosing a tool for a problem that
has not arrived. `config.schema_migrations` records what has been applied.

## Files

| File | What it does | Idempotent |
|---|---|---|
| `0001_config_plane.sql` | Schema, 17 tables, one view, seven guards | No — one transaction, fails atomically if already applied |
| `0002_reference_data.sql` | `step_kinds`, `agent_classes`, `capability_policy` | Yes |
| `0003_model_catalog.sql` | 12 priced models and the three aliases | Yes |
| `0004_prompt_projection.sql` | The engine-projection columns S7 writes | Yes |
| `0005_stage_source.sql` | `agents.stage_source` and the guard that follows from it (S5) | Yes |
| `0001_config_plane_verify.sql` | Attempts every write the schema must refuse | Yes — ends in `ROLLBACK` |

**Apply them in filename order, and run the verify script last.** It is
numbered `0001_*` because it verifies `0001`'s guards, not because it runs
second.

## Applying

```bash
# prep — in this order, verify last
for f in 0001_config_plane 0002_reference_data 0003_model_catalog \
         0004_prompt_projection 0005_stage_source 0001_config_plane_verify; do
  psql "$PREP_DATABASE_URL" -v ON_ERROR_STOP=1 -f "migrations/$f.sql" || break
done
```

`ON_ERROR_STOP=1` is not optional. Without it `psql` keeps going after a
failed statement, and a half-applied migration is worse than a failed one.

The verify script is safe to run anywhere, including prod: everything it
writes is inside one transaction that ends in `ROLLBACK`. Run it after every
apply. A constraint nobody has watched refuse anything is a constraint nobody
knows is wired up. 53 checks; it must end in `ALL CHECKS PASSED`.

It has to be safe on a database with *every* migration applied, and for a
while it was not: it created aliases called `sonnet` and `haiku`, which
`0003` seeds, so on a fully-migrated database it aborted on a duplicate key at
check 9c — twenty checks before the end, with an error that read like a schema
fault rather than a name clash in the test. Its aliases are now named
`verify-*`. The lesson generalises: a verify script that seeds anything must
namespace it, because the one database it most needs to run on is the one that
already has everything.

## What the database refuses

Seven guards, and the reason each is a trigger rather than a code path is the
same: application-level invariants last exactly as long as the next refactor.

| Guard | Rule |
|---|---|
| `agent_versions_00_guard` | A frozen version is immutable and undeletable. Freezing is one-way. |
| `agent_version_steps_00_guard` | The steps of a frozen version cannot be added, changed or removed. |
| `tool_config_00_guard` | Nor can the tool grants of those steps. |
| `prompt_versions_00_guard` | A prompt version is append-only. No update, no delete, no exception for `notes`. |
| `audit_log_00_guard` | The audit log is append-only. |
| `agents_10_pointer_guard` / `client_agent_config_10_pointer_guard` | Only a frozen version can be live or pinned. |
| `agent_version_steps_20_kind_guard` | A step satisfies the `requires_*` flags of its kind. |

The immutability guard compares the whole row as `jsonb` minus `updated_at`,
rather than a list of columns. A column added by a later migration is
therefore frozen by default — the safe direction, and the one a
column-by-column guard gets wrong silently.

## Three places this deviates from the ticket

**1. Two statuses, not three.** SCRUM-217 and SCRUM-218 together say "freeze
the version, mark the previous one superseded, move the pointer" and, of
rollback, "moving a pointer. No data change, no deletion." Both cannot hold if
`superseded` is stored: rolling back would have to un-supersede the version it
restores, which is a data change.

So `agent_versions.status` is `draft` or `frozen`, and which frozen version is
live is `agents.published_version_id` and nothing else. A rollback writes one
uuid. The three-state vocabulary is still available, derived, from
`config.agent_version_state`. When each version was live is in `audit_log`,
with an actor and a timestamp — which a status column never carried.

**2. Two support tables beyond the thirteen.** Each because a constraint
demanded one rather than for tidiness:

* `agent_custom_agent_keys` — C4 §5 requires that each portal key appears
  exactly once across all agents. As a `text[]` column that is a test someone
  has to remember to write; as a table it is the primary key.
* `model_aliases` — S12 needs `haiku`/`sonnet`/`opus` to repoint without a
  redeploy. An alias is a pointer, and a pointer that shares a row with the
  thing it points at cannot move.

**3. Pricing is `NOT NULL` on `models`, not a separate table.** S4 validates
that "the model exists and has a pricing row". Making the price part of the
model makes a priceless model unrepresentable, so there is no row to miss and
no silent fall-through to `DEFAULT_MODEL_PRICING` ($3/$15, Sonnet's) — which
is what bills Opus work at a third of its cost today.

## Two vendor columns, on purpose

`models.vendor` and `models.route` look redundant and are not. The two
repositories use the same word for different questions:

* `vendor` — who makes the model. `agent-middleware`'s `ModelVendor`:
  `anthropic` / `google` / `meta` / …
* `route` — how this deployment reaches it. `agent-engine`'s own
  `ModelVendorSchema`: `anthropic` / `gemini` / `model-garden` /
  `openai-compatible`.

Llama on Model Garden is `vendor: meta, route: model-garden`. Collapsing them
into one column is how a Studio author ends up unable to express a model that
exists.

## What S1 still owes this

The schema is verified against PostgreSQL 16 but has not been applied to Cloud
SQL, because it needs from SCRUM-216:

1. The instance connection name (`project:region:instance`) for prep and prod.
2. A database and a role. The role needs `CREATE` on the database to apply
   this, and the runtime role wants less than that — see below.
3. How the service authenticates: IAM database authentication (no password to
   store) or a password in Secret Manager. The project has zero Secret Manager
   secrets today, so IAM is the shorter path.

### Least privilege, once the instance exists

The migration role and the runtime role should not be the same role. The
runtime never needs DDL, and a runtime role that can `DROP TABLE` is one
injection away from being the only backup that mattered.

```sql
-- runtime: read and write rows, never change shape
grant usage on schema config to "agent-middleware-sa@PROJECT.iam";
grant select, insert, update, delete on all tables in schema config
  to "agent-middleware-sa@PROJECT.iam";
alter default privileges in schema config
  grant select, insert, update, delete on tables
  to "agent-middleware-sa@PROJECT.iam";
```

`audit_log` and `prompt_versions` still refuse `UPDATE` and `DELETE` from that
role — the guards are triggers, so they apply to every role including the
owner. Granting the privilege is not the same as being able to use it, which
is the point.
