# Agent Middleware — the control plane

FastAPI service that owns everything *configurable* about the agent platform, so
the execution engine can stay stateless.

**Before you add a product, a platform or a migration, read
[`docs/AGENT-ARCHITECTURE.md`](docs/AGENT-ARCHITECTURE.md)** — what this service owes an
agent at either end of a run, and why the engine never reads a database.

It is the source of truth in Firestore for:

- **Agents** — identity, model policy, status, tags.
- **System prompts** — immutable, numbered versions with one active at a time.
- **Few-shot examples** — including ones promoted out of reviewer feedback.
- **Templates** — HTML layouts and JSON schemas, versioned, each version
  carrying the `gs://` URIs of the binary assets it renders with.
- **Runs and feedback** — what was dispatched, what came back, and what a
  reviewer thought of it.

## How a job actually runs

```
karosCMO (portal)
      │  POST /agents/{id}/jobs        ← the one dispatch route
      ▼
agent-middleware  ── resolves context (prompt + template + assets + examples)
      │            ── writes the run record
      │            ── publishes ONE message
      ▼
Pub/Sub  (karos-agent-runs-{prep,prod})
      ▼
agent-engine worker  ── stateless: everything it needs is in the message
      │
      └─ PATCH /agents/{id}/runs/{run_id}   ← reports the result back
```

The engine never reads this database. Whatever the message carries is what the
run used, which is what makes feedback attributable to an exact prompt version
and template version.

### The published message carries two shapes

agent-engine validates every message against its own `RunJobRequestSchema`,
which requires three camelCase keys at the **top level** — `clientSlug`,
`productId`, `runKind` — and nacks anything without them (five attempts, then
the dead-letter topic). So `to_engine_message()` in `services/dispatch.py`
emits those three alongside the richer `JobPayload`.

Zod strips unknown keys rather than rejecting them, so the engine reads its
three and ignores the rest. One message therefore satisfies the engine as it is
today *and* carries the resolved prompt, template and assets it will read once
it knows how — no second topic, no versioned cutover. `clientSlug` duplicating
`client_slug` is the deliberate price of that.

`POST /agents/{id}/jobs` requires `client_slug`: the engine resolves a whole
tenant workspace from it and there is no safe default.

### Why there is no generic Pub/Sub bridge

An earlier version of this service relayed arbitrary messages between two
topics — a background pull subscriber plus a `POST /pubsub/push` endpoint. That
path was removed. It knew nothing about agents, recorded no run, and produced
messages that feedback had nothing to attach to; keeping it meant two ways for
work to reach the engine, only one of which was traceable. `POST
/agents/{id}/jobs` is now the only publisher, and `PUBSUB_JOB_TOPIC_ID` the only
destination.

Removed with it: `PUBSUB_SOURCE_SUBSCRIPTION_ID`, `PUBSUB_DESTINATION_TOPIC_ID`,
`ENABLE_PULL_SUBSCRIBER`, `SUBSCRIBER_MAX_MESSAGES`,
`SUBSCRIBER_ACK_DEADLINE_SECONDS`. Set `PUBSUB_JOB_TOPIC_ID` instead — it is now
required.

## Authentication

Every route except `/health/*` requires the caller to prove who it is.

**Production — Google OIDC.** A Cloud Run caller asks its metadata server for an
identity token whose audience is this service's URL and sends it as
`Authorization: Bearer <jwt>`. The signature is verified against Google's
public keys, then the `aud` claim and (optionally) the caller's service-account
email are checked.

`AUTH_AUDIENCE` is mandatory, and the service returns 500 rather than verifying
without it. Google issues valid, correctly-signed identity tokens to every
account on every project, so a signature alone proves nothing — the audience is
what binds a token to *this* service and stops one minted elsewhere being
replayed here.

`AUTH_ALLOWED_SERVICE_ACCOUNTS` is a second gate. Empty, it admits any identity
Google vouches for, which is only safe when Cloud Run IAM (`roles/run.invoker`)
already restricts who can reach the service — the normal deployment. Populate it
for defence in depth.

**Development — a static bearer token.** Set `AUTH_DEV_TOKEN` and send it as a
bearer token. It is ignored outright when `ENVIRONMENT=production`, so a stray
value on a production deploy cannot become a silent auth bypass. A token that
does not match falls through to OIDC verification, so a dev token being
configured never shadows a real caller.

`AUTH_ENABLED=false` turns the whole thing off, for local work only. Startup
logs a warning when it is off, and another when a dev token is live.

Health stays open deliberately: Cloud Run's startup and liveness probes carry no
identity token, and the endpoint exposes only reachability booleans.

## Storage split

| What | Where | Why |
|---|---|---|
| Prompts, guidelines, JSON schemas, HTML layouts | Firestore (`prompts`, `promptVersions`, `templates`, `templateVersions`) | Text, versioned, diffable, read on every dispatch |
| Images, fonts, media | GCS (`gs://<bucket>/templates/...`) | Binary, large, fetched by the engine at render time |

A template version stores only the `gs://` URIs. Bytes never pass through this
service or through a Pub/Sub message. Assets are versioned *with* the body, so
rolling a template back restores the exact asset set that body was authored
against.

Asset URIs must start with `gs://` — an `https://` link or a bare path is
rejected at the API boundary rather than failing later inside a render.

## Architecture

```
app/
  main.py                 App factory, lifespan, exception handlers, auth wiring
  config.py               Settings (pydantic-settings, env-driven)
  security.py             OIDC verification + the dev-token fallback
  dependencies.py         FastAPI dependency providers
  core/
    enums.py              Shared enumerations
    exceptions.py         Domain exceptions -> HTTP status codes
  db/
    firestore.py          The only module that knows the Firestore client
  services/
    agents.py             Agent CRUD + lifecycle
    prompts.py            Prompt versions + few-shot examples
    templates.py          Templates, versions, assets, agent bindings
    context.py            Resolves one agent into one self-contained snapshot
    dispatch.py           Context -> run record -> published message
    runs.py               Run history
    feedback.py           Reviewer verdicts; promotion into examples
    publisher.py          Pub/Sub publisher wrapper
  api/
    routes/               health, agents, prompts, templates, context, runs
    schemas/              Request/response models
scripts/
  seed_legacy_agents.py   Idempotent migration from the karos-agents lab repo
tests/                    Pytest suite; fake Pub/Sub + in-memory Firestore
```

## Key endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live`, `/health/ready` | Probes (unauthenticated) |
| `GET` | `/agents/{id}/context` | Resolve prompt + template + assets + examples |
| `POST` | `/agents/{id}/payload` | Preview the exact payload a dispatch would send |
| `POST` | `/agents/{id}/jobs` | **Dispatch**: resolve, record a run, publish |
| `PATCH` | `/agents/{id}/runs/{run_id}` | Engine reports the result |
| `POST` | `/agents/{id}/runs/{run_id}/feedback` | Reviewer verdict |
| `POST` | `/agents/{id}/feedback/{id}/promote` | Turn feedback into a few-shot example |

Full OpenAPI at `/docs`.

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env    # then set AUTH_ENABLED=false for local work
uvicorn app.main:app --reload --port 8080
```

## Seeding from the legacy lab repo

`scripts/seed_legacy_agents.py` migrates prompts, templates and binary assets
out of the `karos-agents` repo and into Firestore + GCS. It is idempotent — safe
to re-run against prep or prod — and goes through `AgentService` / `PromptService`
/ `TemplateService` rather than writing Firestore directly, so every seeded row
passes the same validation an API caller would.

```bash
# See what it would do, touching nothing:
python scripts/seed_legacy_agents.py --karos-agents ../karos-agents --dry-run

# Against the Firestore emulator:
FIRESTORE_EMULATOR_HOST=localhost:8080 python scripts/seed_legacy_agents.py \
    --karos-agents ../karos-agents

# Against prep (text only; add --upload-assets for the binaries):
python scripts/seed_legacy_agents.py --karos-agents ../karos-agents --env prep
```

Run `--help` for the full flag set. The script reports what it created, what it
skipped as already-current, and — importantly — warns about any prompt that
still contains Claude-specific scaffolding a human needs to rewrite. It strips
YAML frontmatter mechanically but never rewrites prose, because "make this
model-agnostic" is a judgment call, not a substitution.

## Tests

```bash
pytest              # 80 tests, no credentials, no network, no emulator
ruff check .
mypy app
```

## Docker

```bash
docker build -t agent-middleware .
docker run --rm -p 8080:8080 --env-file .env agent-middleware
```
