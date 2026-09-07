# The other two halves of S12 — what agent-engine and karosCMO need

SCRUM-222 is scoped to `agent-middleware`, and the catalog side is done here.
Two hard-coded pricing tables remain, in repositories this ticket does not
touch. This is what they need, and why.

Pairs with **T-B23** on the portal side.

---

## The finding that is bigger than the ticket

The ticket says a pricing miss falls through to Sonnet's $3/$15. True, and
there is a worse problem underneath it: **three of the prices that are not
missing are wrong.**

Verified against `platform.claude.com/docs/en/about-claude/pricing` on
2026-09-04, and cross-checked against an independent source dated 2026-09-02:

| Model | Both tables say | Published | Effect |
|---|---|---|---|
| `claude-opus-4-8` | $15 / $75 | **$5 / $25** | every Opus step overstated **3×** |
| `claude-opus-4-7` | $15 / $75 | **$5 / $25** | same |
| `claude-haiku-4-5-*` | $0.80 / $4 | **$1 / $5** | guardrail spend understated ~25% |

Anthropic cut Opus pricing; the tables were written before that and have no
date on them, which is why nobody noticed. Every cost report, every client
margin figure and every "which agent is expensive" ranking that involves Opus
is wrong by a factor of three today.

The two rows the ticket names as *missing* — Opus 5 and Sonnet 5 — are
genuinely missing, and the `conftest` agent fixture already names
`claude-opus-5`.

## agent-engine

### 1. `pricingForModel` must throw

`packages/core/src/telemetry/pricing.ts`:

```ts
export function pricingForModel(modelName: string): ModelPricing {
  // …the two spelling normalisations stay: they are correct and they are not
  // the problem.
  const undated = canonical.replace(/-\d{8}$/, "");
  const row = MODEL_PRICING[undated];
  if (!row) {
    throw new UnpricedModelError(modelName);   // was: ?? DEFAULT_MODEL_PRICING
  }
  return row;
}
```

`DEFAULT_MODEL_PRICING` goes away entirely. It has no correct use: a model
whose price is unknown produces an unknown cost, and $3/$15 is not an estimate
of that — it is a specific wrong number that happens to look plausible.

Where to catch it matters. A throw inside `computeStepCostUsd` would fail a
*run* over a *bookkeeping* problem, which is the wrong trade: the deliverable
is fine and the client is waiting. Suggested shape — record the step with
`costUsd: null` and a `pricingError`, let the run finish, and surface the gap
in telemetry. A null in a cost column is honest and visibly aggregates to
"unknown"; a plausible number does not.

### 2. Correct the three rows above, and add the missing ones

```ts
"claude-opus-5":              { inputPer1M: 5.0,  outputPer1M: 25.0, cachedInputPer1M: 0.50 },
"claude-opus-4-8":            { inputPer1M: 5.0,  outputPer1M: 25.0, cachedInputPer1M: 0.50 },
"claude-opus-4-7":            { inputPer1M: 5.0,  outputPer1M: 25.0, cachedInputPer1M: 0.50 },
"claude-sonnet-5":            { inputPer1M: 2.0,  outputPer1M: 10.0, cachedInputPer1M: 0.20 },
"claude-sonnet-4-6":          { inputPer1M: 3.0,  outputPer1M: 15.0, cachedInputPer1M: 0.30 },
"claude-haiku-4-5":           { inputPer1M: 1.0,  outputPer1M: 5.0,  cachedInputPer1M: 0.10 },
"claude-haiku-4-5-20251001":  { inputPer1M: 1.0,  outputPer1M: 5.0,  cachedInputPer1M: 0.10 },
"gemini-2.5-pro":             { inputPer1M: 1.25, outputPer1M: 10.0 },
"gemini-2.5-flash":           { inputPer1M: 0.30, outputPer1M: 2.50 },
"gemini-2.5-flash-lite":      { inputPer1M: 0.10, outputPer1M: 0.40 },
"llama-3.3-70b-instruct-maas":{ inputPer1M: 0.72, outputPer1M: 0.72 },
"mistral-small-2503":         { inputPer1M: 0.10, outputPer1M: 0.30 },
"mistral-medium-3":           { inputPer1M: 0.40, outputPer1M: 2.00 },
```

Note the explicit `cachedInputPer1M` on the Claude rows. `CACHE_READ_DISCOUNT
= 0.1` happens to be exactly right for all of them, so nothing changes today —
but Anthropic already ships models at a different cache multiplier (Fable and
Mythos 5.1 read cache at 0.025× base), and a hard-coded 0.1 will be wrong the
first time one of those is routed. Stating the number removes the assumption.

`gpt-4o` and `gpt-4o-mini` are deliberately **not** in that list — see below.

### 3. Move the tertiary fallback off `gemini-1.5-flash`

`create-model-router-from-env.ts` defaults `CLAUDE_FALLBACK_GEMINI_MODEL` to
`gemini-1.5-flash`. That is the one hop that genuinely changes model identity,
and Vertex prices that model **per 1,000 characters**, not per token — so there
is no honest per-token row for it. Converted at Google's own 4-chars-per-token
guidance it is about **$0.075 / $0.30**, against the $3 / $15 the engine has
been assuming: **40× and 50× overstated.**

`gemini-2.5-flash-lite` is token-priced at $0.10 / $0.40, cheaper in practice,
and needs no conversion. Changing one env var default removes a whole class of
wrong number.

### 4. Read the alias table from the control plane

`GET /models/aliases` now serves `haiku` / `sonnet` / `opus`, seeded to match
`MODEL_ALIASES` exactly — so this is a no-op in behaviour and the point is that
the next model generation stops being a code change and a redeploy.

One disagreement to resolve rather than paper over: the engine points `opus` at
`claude-opus-4-8` and marks it `pinned`, while the catalog marks that model
`not_enabled` — so **the alias resolves to a model this deployment does not
route.** Fixing it is either enabling the model or repointing the alias, and
both are decisions, so the seed preserves the disagreement instead of quietly
picking one.

## karosCMO (T-B23)

`src/lib/models/usage-log.ts`:

1. Correct the same three Claude rows.
2. **Delete `MODEL_PRICING._default`.** `computeCostUsd` currently does
   `MODEL_PRICING[modelName] ?? MODEL_PRICING._default!`, so every unknown
   model in the portal's own ledger is billed at Sonnet's rate. Same reasoning
   as above: return `null` and render "unknown", which aggregates honestly.
3. Prefer `GET /models/{id}` over the local table once the catalog is seeded.
   The middleware refuses to serve an unpriced row, so a 404 there is the same
   signal as a miss here — and there is then one place a price is written down.

### `gpt-4o` / `gpt-4o-mini` need a decision, not a copy

Both are in the portal's table at $2.50/$10 and $0.15/$0.60, and **neither
appears on OpenAI's current pricing page** (checked 2026-09-04) — the listed
models are a later generation entirely. So those two numbers cannot be verified
from the primary source, and the SEO/GEO "chatgpt" engine's cost is
unattributable until someone confirms what those calls actually bill.

They are deliberately absent from the seeded catalog for that reason: under
this ticket's own principle, an unverifiable price is worse than an absent one,
because it produces a number people act on. Confirm the current prices (or the
current model), then add them.

## What the middleware now offers the other two

| Endpoint | For |
|---|---|
| `GET /models/{id}` | one model, priced, with `pricing_source` and `pricing_checked_on` |
| `GET /models` | the catalog, `available` first |
| `GET /models/aliases` | `haiku` / `sonnet` / `opus` → model + `provider_model_name` + region |
| `GET /models/pricing-coverage` | every model any agent names, and which of those cannot be priced |

`pricing-coverage` is the pre-flight. Run it before turning
`MODEL_PRICING_ENFORCED=true` on in an environment: with the catalog unseeded,
enforcement turns every dispatch into a 422, and the way that gets noticed is a
client asking why nothing ran.

## Order of operations per environment

```bash
# 1. what is missing right now
curl -s .../models/pricing-coverage | jq '.gaps'

# 2. seed the catalog (idempotent; --dry-run first)
python scripts/seed_models.py --env prep --dry-run
python scripts/seed_models.py --env prep

# 3. confirm the gaps are gone
curl -s .../models/pricing-coverage | jq '.gaps | length'   # expect 0

# 4. only then
#    MODEL_PRICING_ENFORCED=true
```

Step 3 returning anything but `0` means an agent names a model nobody has
priced. That is a real gap, not a reason to skip step 4 — the coverage response
names the agent and the stage.
