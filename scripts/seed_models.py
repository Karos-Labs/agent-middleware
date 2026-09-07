#!/usr/bin/env python3
"""Seed the normalized model catalog, its prices and its aliases.

Agent stages used to name a model as free text. This populates the ``models``
collection those stages reference instead, with the models this platform routes
today plus the ones it deliberately does not.

Listing what is NOT enabled is the point of the second group. A catalog showing
only what works reads as "this is everything the vendor has", and someone
concludes a model is unavailable when it is one config change away. Those rows
render disabled in the Studio with a "Request access" action.

``provider_model_name`` is kept separate from the document id because they are
not the same string: Claude served through Vertex is published under a
different name from Claude on Anthropic's own API, and the engine's router
needs the one it will actually send.

Idempotent by content comparison, so a re-run writes nothing when unchanged.

## S12 (SCRUM-222): prices and aliases live here now

Two things moved into this file, and one thing deliberately did not.

**MODEL_PRICING moved.** It was 17 rows hard-coded in
``agent-engine/packages/core/src/telemetry/pricing.ts`` and 12 more in
``karosCMO/src/lib/models/usage-log.ts``, and both answer an unrecognised model
with Sonnet's $3/$15 and no signal. A plausible wrong number is the worst
failure available in a cost report, because nothing about it looks broken.

**Three of those hard-coded prices are wrong today**, which is a stronger
finding than the ticket's ("a miss falls back to Sonnet"). Verified against
platform.claude.com/docs/en/about-claude/pricing on 2026-09-04:

    claude-opus-4-8    tables say $15/$75   actually $5/$25   3x overstated
    claude-opus-4-7    tables say $15/$75   actually $5/$25   3x overstated
    claude-haiku-4-5   tables say $0.80/$4  actually $1/$5    understated

So every Opus step in every cost report the platform has ever produced is
overstated threefold. The rows below carry the verified numbers.

**MODEL_ALIASES moved.** ``haiku`` / ``sonnet`` / ``opus`` were three
``as const`` lines in ``router/aliases.ts``, which made a new model generation
a code change and a redeploy -- precisely what an alias exists to prevent.

**What is deliberately NOT here, and why:**

* ``gpt-4o`` and ``gpt-4o-mini``. Both are in karosCMO's pricing table at
  $2.50/$10 and $0.15/$0.60, and *neither appears on OpenAI's current pricing
  page* (checked 2026-09-04) -- the listed models are a later generation
  entirely. Their prices are therefore unverifiable from the primary source,
  and the whole point of this ticket is that an unverifiable price is worse
  than an absent one. The SEO/GEO "chatgpt" engine's cost is consequently
  unattributable until someone confirms what those calls actually bill; that
  belongs to T-B23 on the portal side.
* ``claude-3-5-sonnet-20241022``, ``claude-3-opus-20240229``,
  ``claude-3-haiku-20240307`` and friends. Historical models, not on the
  current price list, and no agent names any of them. The engine keeps its rows
  for costing old telemetry, which is the right place for them.
* ``llama-3-1-70b-instruct-maas``, which this file used to seed. Google does
  not publish a price for it (delisted, renamed, or priced on request), so
  under a schema where a model must be priceable it does not belong in the
  catalog. ``llama-3-3-70b-instruct-maas`` replaces it -- same role, and a
  published price.

## The Gemini fallback is the expensive one

``create-model-router-from-env.ts`` uses ``gemini-1.5-flash`` as the tertiary
fallback -- the one hop that actually changes model identity. Vertex prices
that model **per 1,000 characters**, not per token, so there is no honest way
to key it into a per-token table; the row below converts at Google's own
4-characters-per-token guidance and says so in ``pricing_source``.

The conversion lands at $0.075/$0.30 per 1M tokens. Against the $3/$15 the
engine silently assumed, that is **40x and 50x overstated**.

The real fix is to point ``CLAUDE_FALLBACK_GEMINI_MODEL`` at
``gemini-2.5-flash-lite``, which is token-priced ($0.10/$0.40, verified) and so
needs no conversion at all. The row for it is here so that change is one env
var away.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

ENVIRONMENTS: dict[str, str] = {"prep": "prep", "prod": "(default)"}
FIRESTORE_PROJECT = "karoscmo"
COLLECTION = "models"
ALIAS_COLLECTION = "model_aliases"

#: The day every price below was read off its source. One date for the whole
#: file rather than one per row: they were all checked in the same sitting, and
#: a per-row date that is really a copy of this one is a date nobody trusts.
PRICES_CHECKED_ON = "2026-09-04"

CLAUDE_PRICES = "platform.claude.com/docs/en/about-claude/pricing"
VERTEX_PRICES = "cloud.google.com/vertex-ai/generative-ai/pricing"

#: The catalog. `available` means the vendor offers it AND agent-engine's
#: router is wired for it; `not_enabled` means only the first half is true.
CATALOG: tuple[dict[str, Any], ...] = (
    # --- Anthropic, served through Vertex / Agent Platform -------------------
    {
        "model_id": "claude-sonnet-4-6-on-vertex",
        "display_name": "Claude Sonnet 4.6 (Vertex)",
        "vendor": "anthropic",
        "route": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-sonnet-4-6",
        "region": "global",
        "description": "The default drafting model for every hand-written agent in agent-engine.",
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned", "portable"],
        "input_per_1m": 3.0,
        "output_per_1m": 15.0,
        "cached_input_per_1m": 0.30,
        "pricing_source": CLAUDE_PRICES,
    },
    {
        "model_id": "claude-haiku-4-5-on-vertex",
        "display_name": "Claude Haiku 4.5 (Vertex)",
        "vendor": "anthropic",
        "route": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-haiku-4-5-20251001",
        "region": "global",
        "description": (
            "Classification and gating. What the dynamic runner's topic-guardrail "
            "verifier uses, which is why having guardrails on costs almost nothing."
        ),
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "input_per_1m": 1.0,
        "output_per_1m": 5.0,
        "cached_input_per_1m": 0.10,
        "pricing_source": CLAUDE_PRICES,
        "notes": (
            "Both hard-coded tables carry $0.80/$4.00 for this model. The published "
            "price is $1/$5, so guardrail and classification spend has been "
            "understated by a quarter."
        ),
    },
    {
        "model_id": "claude-opus-5-on-vertex",
        "display_name": "Claude Opus 5 (Vertex)",
        "vendor": "anthropic",
        "route": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-opus-5",
        "region": "global",
        "description": "Current highest-capability Anthropic tier.",
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "input_per_1m": 5.0,
        "output_per_1m": 25.0,
        "cached_input_per_1m": 0.50,
        "pricing_source": CLAUDE_PRICES,
        "notes": (
            "Named by the conftest agent fixture and absent from both pricing tables, "
            "so any run on it was costed at the Sonnet fallback. Not routed yet: "
            "enabling it is a spend decision, not a config one."
        ),
    },
    {
        "model_id": "claude-sonnet-5-on-vertex",
        "display_name": "Claude Sonnet 5 (Vertex)",
        "vendor": "anthropic",
        "route": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-sonnet-5",
        "region": "global",
        "description": "Current Sonnet generation.",
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned", "portable"],
        "input_per_1m": 2.0,
        "output_per_1m": 10.0,
        "cached_input_per_1m": 0.20,
        "pricing_source": CLAUDE_PRICES,
        "notes": (
            "$2/$10 is what the price list showed on 2026-09-04. At least one "
            "secondary source reports it as an introductory rate that reverts to "
            "$3/$15; re-check before this row is relied on for a quote, which is "
            "what pricing_checked_on is for."
        ),
    },
    {
        "model_id": "claude-opus-4-8-on-vertex",
        "display_name": "Claude Opus 4.8 (Vertex)",
        "vendor": "anthropic",
        "route": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-opus-4-8",
        "region": "global",
        "description": "Previous highest-capability Anthropic tier.",
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "input_per_1m": 5.0,
        "output_per_1m": 25.0,
        "cached_input_per_1m": 0.50,
        "pricing_source": CLAUDE_PRICES,
        "notes": (
            "Both hard-coded tables carry $15/$75 for this model -- three times the "
            "published price. Every Opus step in every cost report is overstated "
            "threefold until those tables are corrected."
        ),
    },
    # --- Google -------------------------------------------------------------
    {
        "model_id": "gemini-2-5-pro",
        "display_name": "Gemini 2.5 Pro",
        "vendor": "google",
        "route": "gemini",
        "availability": "available",
        "provider_model_name": "gemini-2.5-pro",
        "region": "us-central1",
        "description": "Long-context reasoning. Wired through the same Vertex router as Claude.",
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["portable"],
        "input_per_1m": 1.25,
        "output_per_1m": 10.0,
        "pricing_source": VERTEX_PRICES,
    },
    {
        "model_id": "gemini-2-5-flash",
        "display_name": "Gemini 2.5 Flash",
        "vendor": "google",
        "route": "gemini",
        "availability": "available",
        "provider_model_name": "gemini-2.5-flash",
        "region": "us-central1",
        "description": "Cheap, fast extraction and classification.",
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "input_per_1m": 0.30,
        "output_per_1m": 2.50,
        "pricing_source": VERTEX_PRICES,
        "notes": "Text/image/video input. Audio input is priced separately at $1.00/1M.",
    },
    {
        "model_id": "gemini-2-5-flash-lite",
        "display_name": "Gemini 2.5 Flash Lite",
        "vendor": "google",
        "route": "gemini",
        "availability": "available",
        "provider_model_name": "gemini-2.5-flash-lite",
        "region": "us-central1",
        "description": (
            "The cheapest token-priced model on the platform. Seeded so the engine's "
            "tertiary fallback can move off gemini-1.5-flash, which Vertex prices "
            "per character and nothing can cost honestly."
        ),
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "input_per_1m": 0.10,
        "output_per_1m": 0.40,
        "pricing_source": VERTEX_PRICES,
    },
    {
        "model_id": "gemini-1-5-flash",
        "display_name": "Gemini 1.5 Flash",
        "vendor": "google",
        "route": "gemini",
        "availability": "available",
        "provider_model_name": "gemini-1.5-flash",
        "region": "us-central1",
        "description": (
            "agent-engine's TERTIARY fallback (CLAUDE_FALLBACK_GEMINI_MODEL default) "
            "-- the one hop that changes model identity, and the one nothing could "
            "price."
        ),
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "input_per_1m": 0.075,
        "output_per_1m": 0.30,
        "pricing_source": (
            f"{VERTEX_PRICES} -- Vertex prices this model PER 1,000 CHARACTERS "
            "($0.00001875 in / $0.000075 out), not per token. Converted at Google's "
            "own 4-characters-per-token guidance; approximate by construction."
        ),
        "notes": (
            "The engine assumed $3/$15 for this model, so its fallback hops were "
            "costed 40x and 50x over. Preferred fix: point "
            "CLAUDE_FALLBACK_GEMINI_MODEL at gemini-2.5-flash-lite, which is "
            "token-priced and needs no conversion."
        ),
    },
    # --- Model Garden partner models ----------------------------------------
    {
        "model_id": "llama-3-3-70b-instruct-maas",
        "display_name": "Llama 3.3 70B Instruct (MaaS)",
        "vendor": "meta",
        "route": "model-garden",
        "availability": "not_enabled",
        "provider_model_name": "meta/llama-3.3-70b-instruct-maas",
        "region": "us-central1",
        "description": "Open-weights option served through Vertex Model-as-a-Service.",
        "context_window": 128_000,
        "supports_tools": False,
        "tiers": ["commodity"],
        "input_per_1m": 0.72,
        "output_per_1m": 0.72,
        "pricing_source": VERTEX_PRICES,
        "notes": (
            "Replaces llama-3-1-70b-instruct-maas, which this file used to seed and "
            "for which Google publishes no price. Note supports_tools is false: a "
            "stage granting tools would not work on it even once enabled."
        ),
    },
    {
        "model_id": "mistral-small-2503",
        "display_name": "Mistral Small 3.1 (25.03)",
        "vendor": "mistral",
        "route": "model-garden",
        "availability": "not_enabled",
        "provider_model_name": "mistral-small-2503",
        "region": "us-central1",
        "description": "Cheap open-weights alternative for commodity steps.",
        "context_window": 128_000,
        "supports_tools": False,
        "tiers": ["commodity"],
        "input_per_1m": 0.10,
        "output_per_1m": 0.30,
        "pricing_source": VERTEX_PRICES,
    },
    {
        "model_id": "mistral-medium-3",
        "display_name": "Mistral Medium 3",
        "vendor": "mistral",
        "route": "model-garden",
        "availability": "not_enabled",
        "provider_model_name": "mistral-medium-3",
        "region": "us-central1",
        "description": "Mid-tier open-weights option.",
        "context_window": 128_000,
        "supports_tools": False,
        "tiers": ["portable"],
        "input_per_1m": 0.40,
        "output_per_1m": 2.00,
        "pricing_source": VERTEX_PRICES,
    },
)

#: Models this file used to seed and no longer does. Same rule as
#: seed_all_agents.RETIRED: dropping an entry from CATALOG does not remove the
#: document, so retiring one has to be an action rather than an omission.
RETIRED: dict[str, str] = {
    "llama-3-1-70b-instruct-maas": (
        "Google publishes no price for it, and an unpriceable model is exactly the "
        "row S12 exists to remove. Superseded by llama-3-3-70b-instruct-maas."
    ),
}

#: The Studio's three-option picker, matching agent-engine's MODEL_ALIASES
#: exactly so moving the resolution here changes nothing about what runs.
#:
#: `opus` is worth a second look: the engine points it at claude-opus-4-8 and
#: marks it `pinned`, while this catalog marks that model `not_enabled`. So the
#: alias resolves to a model this deployment does not route. Preserved as-is
#: rather than quietly repointed -- the two lists disagreeing is a finding, and
#: fixing it is either enabling the model or changing the alias, both of which
#: are decisions.
ALIASES: tuple[dict[str, Any], ...] = (
    {
        "alias": "haiku",
        "model_id": "claude-haiku-4-5-on-vertex",
        "provider_policy": "commodity",
        "description": "Classification, extraction, sorting, dedupe similarity.",
    },
    {
        "alias": "sonnet",
        "model_id": "claude-sonnet-4-6-on-vertex",
        "provider_policy": "pinned",
        "description": "The default for writing and judgment -- reaches a client, stays pinned.",
    },
    {
        "alias": "opus",
        "model_id": "claude-opus-4-8-on-vertex",
        "provider_policy": "pinned",
        "description": "Reserved for when exact phrasing is the deliverable itself.",
    },
)


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)

    def record(self, outcome: str, what: str) -> None:
        self.counts[outcome] += 1
        symbols = {"created": "+", "updated": "~", "unchanged": "=", "retired": "-"}
        print(f"  {symbols.get(outcome, '?')} {outcome:<9} {what}")


def _comparable(row: dict[str, Any]) -> str:
    """Everything except the timestamps, so an unchanged row is recognised as one."""
    return json.dumps(
        {k: v for k, v in sorted(row.items()) if k not in {"created_at", "updated_at"}},
        ensure_ascii=False,
        sort_keys=True,
    )


def _full_row(entry: dict[str, Any]) -> dict[str, Any]:
    """One catalog document, with every optional field present.

    Explicit nulls rather than absent keys: a reader distinguishing "no price"
    from "this row predates prices" is the difference between a gap it can
    report and one it has to infer.
    """

    document = {"id": entry["model_id"], **entry}
    document.setdefault("notes", None)
    document.setdefault("description", None)
    document.setdefault("cached_input_per_1m", None)
    document["pricing_checked_on"] = PRICES_CHECKED_ON
    return document


def _check_prices_are_sane() -> list[str]:
    """Catch the transcription mistakes that produce a plausible wrong number.

    Runs before anything is written, because the failure this whole ticket is
    about is a price that looks fine. A digit dropped from an output price is
    invisible in review and wrong in every report afterwards.
    """

    problems: list[str] = []
    seen: set[str] = set()
    for entry in CATALOG:
        model_id = entry["model_id"]
        if model_id in seen:
            problems.append(f"{model_id}: listed twice")
        seen.add(model_id)

        for key in ("input_per_1m", "output_per_1m", "pricing_source"):
            if entry.get(key) in (None, ""):
                problems.append(f"{model_id}: {key} is missing")

        inp, out = entry.get("input_per_1m"), entry.get("output_per_1m")
        if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
            if inp <= 0 or out <= 0:
                problems.append(f"{model_id}: a price of zero or less is not a price")
            # Every model on every vendor's list charges more for output than
            # for input. A row that does not is a swapped pair.
            if out < inp:
                problems.append(
                    f"{model_id}: output ({out}) is cheaper than input ({inp}) -- "
                    "the pair is probably swapped"
                )
        cached = entry.get("cached_input_per_1m")
        if isinstance(cached, (int, float)) and isinstance(inp, (int, float)) and cached > inp:
            problems.append(f"{model_id}: a cache read costs more than a fresh read")

    known = {entry["model_id"] for entry in CATALOG}
    for alias in ALIASES:
        if alias["model_id"] not in known:
            problems.append(
                f"alias '{alias['alias']}' points at {alias['model_id']}, which is not "
                "in the catalog"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    problems = _check_prices_are_sane()
    if problems:
        print("The catalog in this file does not check out:\n")
        for problem in problems:
            print(f"  ! {problem}")
        return 1

    try:
        from google.cloud import firestore  # type: ignore[attr-defined]
    except ImportError:
        sys.exit("google-cloud-firestore is not installed in this environment")

    database = ENVIRONMENTS[args.env]
    db = firestore.Client(project=FIRESTORE_PROJECT, database=database)

    print(f"Seeding the model catalog into {FIRESTORE_PROJECT}/{database}")
    print(f"  mode    : {'DRY RUN' if args.dry_run else 'WRITING'}")
    print(f"  models  : {len(CATALOG)} live, {len(RETIRED)} retired")
    print(f"  aliases : {len(ALIASES)}")
    print(f"  prices  : checked {PRICES_CHECKED_ON}\n")

    report = Report()
    now = firestore.SERVER_TIMESTAMP

    for model_id, why in RETIRED.items():
        ref = db.collection(COLLECTION).document(model_id)
        existing = ref.get()
        if not existing.exists:
            continue
        current = existing.to_dict() or {}
        if current.get("availability") == "retired":
            report.record("unchanged", f"{model_id} (already retired)")
            continue
        if not args.dry_run:
            # Kept, not deleted: a stage or an old run may reference it, and a
            # dangling model id in that history is worse than a retired row.
            ref.set(
                {"availability": "retired", "retired_reason": why, "updated_at": now},
                merge=True,
            )
        report.record("retired", f"{model_id} -- {why}")

    for entry in CATALOG:
        document = _full_row(entry)
        label = (
            f"{entry['model_id']} ({entry['availability']}, "
            f"${entry['input_per_1m']}/${entry['output_per_1m']})"
        )

        if args.dry_run:
            report.record("created", label)
            continue

        ref = db.collection(COLLECTION).document(entry["model_id"])
        existing = ref.get()
        if existing.exists:
            current = existing.to_dict() or {}
            if _comparable({**current, "id": entry["model_id"]}) == _comparable(document):
                report.record("unchanged", label)
                continue
            ref.set({**document, "updated_at": now}, merge=True)
            report.record("updated", label)
            continue

        ref.set({**document, "created_at": now, "updated_at": now})
        report.record("created", label)

    for alias in ALIASES:
        document = {"id": alias["alias"], **alias}
        document.setdefault("description", None)
        label = f"{alias['alias']} -> {alias['model_id']}"

        if args.dry_run:
            report.record("created", label)
            continue

        ref = db.collection(ALIAS_COLLECTION).document(alias["alias"])
        existing = ref.get()
        if existing.exists:
            current = existing.to_dict() or {}
            if _comparable({**current, "id": alias["alias"]}) == _comparable(document):
                report.record("unchanged", label)
                continue
            ref.set({**document, "updated_at": now}, merge=True)
            report.record("updated", label)
            continue

        ref.set({**document, "created_at": now, "updated_at": now})
        report.record("created", label)

    print("\n" + "-" * 60)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(report.counts.items()))
    print("summary: " + (summary or "nothing to do"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
