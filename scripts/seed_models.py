#!/usr/bin/env python3
"""Seed the normalized model catalog.

Agent stages used to name a model as free text. This populates the ``models``
collection those stages reference instead, with the Vertex-served models this
platform routes today plus the ones it deliberately does not.

Listing what is NOT enabled is the point of the second group. A catalog showing
only what works reads as "this is everything Vertex has", and someone concludes
a model is unavailable when it is one config change away. Those rows render
disabled in the Studio with a "Request access" action.

``provider_model_name`` is kept separate from the document id because they are
not the same string: Claude served through Vertex is published under a
different name from Claude on Anthropic's own API, and the engine's router
needs the one it will actually send.

Idempotent by content hash, so a re-run writes nothing when unchanged.
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

#: How each vendor's route fails over, in the engine's own terms
#: (`packages/core/src/router/create-model-router-from-env.ts`,
#: `ResilientClaudeAdapter`). Stated once here and copied onto every row so a
#: Studio author reads it beside the model they are about to pick.
CLAUDE_FALLBACK = (
    "Vertex AI (Agent Platform, global endpoint) first. On a 429 or 404 the SAME model is "
    "retried on Anthropic's direct API; if that fails too, Gemini 2.5 Flash answers as the "
    "last resort. Any other error fails the step. Note: as of 2026-09 every Claude model "
    "returns 429 on Vertex in both projects (no quota granted), so in practice these run on "
    "the direct Anthropic API."
)
GEMINI_FALLBACK = (
    "None. Gemini is served by Vertex AI only; a failure on Vertex fails the step, there "
    "is no second transport for these models."
)
NOT_ROUTED = (
    "Not routed in this deployment: no adapter is configured for this vendor, so a "
    "stage pointed at it fails before the first call."
)
LEGACY_CLAUDE = (
    "Previous Claude generation. Kept in the engine's catalog so old run records resolve; "
    "not offered for new stages. Prefer Claude Sonnet 4.6 or Opus 4.8."
)

#: The catalog, mirroring agent-engine's `MODEL_CAPABILITIES`
#: (`packages/core/src/router/model-capabilities.ts`) row for row: that table is
#: the sole authority for model identity inside the engine, and a stage
#: override naming a model it lacks is refused before the run starts. So every
#: row here carries the engine's canonical id as `provider_model_name`, and
#: `available` means the engine both catalogues AND routes it here;
#: `not_enabled` means the engine knows the id but this deployment wires no
#: adapter for its vendor. Listing what is NOT enabled is the point of the
#: second group: a catalog showing only what works reads as "this is everything
#: Vertex has", and someone concludes a model is unavailable when it is one
#: config change away. Those rows render disabled in the Studio with a
#: "Request access" action.
CATALOG: tuple[dict[str, Any], ...] = (
    # ── Anthropic, routed (Claude via Vertex, Anthropic API as the fallback) ──
    {
        "model_id": "claude-opus-4-8-on-vertex",
        "display_name": "Claude Opus 4.8",
        "vendor": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-opus-4-8",
        "region": "global",
        "description": (
            "Highest-capability Anthropic tier. The engine's default for the steps where exact "
            "prose is the deliverable: the newsletter draft and editor, the landing page "
            "blueprint and craft verdict, the Reddit draft. $15 / $75 per 1M tokens."
        ),
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-opus-4-7-on-vertex",
        "display_name": "Claude Opus 4.7",
        "vendor": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-opus-4-7",
        "region": "global",
        "description": (
            "The previous Opus snapshot, same price as 4.8. No engine step defaults to it."
        ),
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-sonnet-4-6-on-vertex",
        "display_name": "Claude Sonnet 4.6",
        "vendor": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-sonnet-4-6",
        "region": "global",
        "description": (
            "The default drafting and judgment model for most hand-written steps in agent-engine "
            "(X, LinkedIn, Instagram, TikTok copy, blog, reputation, SEO/GEO, newsletter plan). "
            "$3 / $15 per 1M tokens."
        ),
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned", "portable"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-haiku-4-5-on-vertex",
        "display_name": "Claude Haiku 4.5",
        "vendor": "anthropic",
        "availability": "available",
        "provider_model_name": "claude-haiku-4-5-20251001",
        "region": "global",
        "description": (
            "Classification and gating: the reputation review classifier, the Instagram language "
            "fluency check, the topic guardrail. $0.80 / $4 per 1M tokens."
        ),
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "fallback": CLAUDE_FALLBACK,
    },
    # ── Google, routed (Vertex only, no fallback) ──
    {
        "model_id": "gemini-2-5-pro",
        "display_name": "Gemini 2.5 Pro",
        "vendor": "google",
        "availability": "available",
        "provider_model_name": "gemini-2.5-pro",
        "region": "global",
        "description": (
            "Long-context reasoning and video understanding (1M-token window). The engine's "
            "default "
            "for the TikTok topic scout and moment selection. $1.25 / $10 per 1M tokens."
        ),
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["portable"],
        "fallback": GEMINI_FALLBACK,
    },
    {
        "model_id": "gemini-2-5-flash",
        "display_name": "Gemini 2.5 Flash",
        "vendor": "google",
        "availability": "available",
        "provider_model_name": "gemini-2.5-flash",
        "region": "global",
        "description": (
            "Cheap, fast extraction, vision and classification: Instagram research, image vetting "
            "and visual QA. Also the engine's last-resort fallback for Claude steps. $0.30 / $2.50 "
            "per 1M tokens."
        ),
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "fallback": GEMINI_FALLBACK,
    },
    {
        "model_id": "gemini-3-1-pro-preview",
        "display_name": "Gemini 3.1 Pro (preview)",
        "vendor": "google",
        "availability": "available",
        "provider_model_name": "gemini-3.1-pro-preview",
        "region": "global",
        "description": (
            "Google's current frontier Pro model, with a 65k-token output window. The engine's "
            "default for the landing page build and fix steps, whose output is a whole front-end. "
            "$2 / $12 per 1M tokens."
        ),
        "context_window": 1_000_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": GEMINI_FALLBACK,
        "notes": "A preview model: Google may change or retire it without notice.",
    },
    # ── Anthropic, previous generation (catalogued, not offered) ──
    {
        "model_id": "claude-3-5-sonnet-on-vertex",
        "display_name": "Claude 3.5 Sonnet (Oct 2024)",
        "vendor": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-3-5-sonnet-20241022",
        "region": "global",
        "description": LEGACY_CLAUDE,
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-3-5-sonnet-v2-on-vertex",
        "display_name": "Claude 3.5 Sonnet v2",
        "vendor": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-3-5-sonnet-v2-20241022",
        "region": "global",
        "description": LEGACY_CLAUDE,
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-3-5-haiku-on-vertex",
        "display_name": "Claude 3.5 Haiku",
        "vendor": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-3-5-haiku-20241022",
        "region": "global",
        "description": LEGACY_CLAUDE,
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-3-opus-on-vertex",
        "display_name": "Claude 3 Opus",
        "vendor": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-3-opus-20240229",
        "region": "global",
        "description": LEGACY_CLAUDE,
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["pinned"],
        "fallback": CLAUDE_FALLBACK,
    },
    {
        "model_id": "claude-3-haiku-on-vertex",
        "display_name": "Claude 3 Haiku",
        "vendor": "anthropic",
        "availability": "not_enabled",
        "provider_model_name": "claude-3-haiku-20240307",
        "region": "global",
        "description": LEGACY_CLAUDE + " Structured output is best-effort on this generation.",
        "context_window": 200_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "fallback": CLAUDE_FALLBACK,
    },
    # ── OpenAI-compatible (catalogued by the engine, no adapter wired here) ──
    {
        "model_id": "gpt-4o",
        "display_name": "GPT-4o",
        "vendor": "other",
        "availability": "not_enabled",
        "provider_model_name": "gpt-4o",
        "region": "global",
        "description": (
            "OpenAI's GPT-4o through the engine's OpenAI-compatible adapter. $2.50 / $10 "
            "per 1M tokens."
        ),
        "context_window": 128_000,
        "supports_tools": True,
        "tiers": ["portable"],
        "fallback": NOT_ROUTED,
        "notes": (
            "Needs an OpenAI-compatible endpoint and key configured on the engine "
            "(MODEL_PROVIDER wiring) before a stage can use it."
        ),
    },
    {
        "model_id": "gpt-4o-mini",
        "display_name": "GPT-4o mini",
        "vendor": "other",
        "availability": "not_enabled",
        "provider_model_name": "gpt-4o-mini",
        "region": "global",
        "description": "OpenAI's small GPT-4o tier. $0.15 / $0.60 per 1M tokens.",
        "context_window": 128_000,
        "supports_tools": True,
        "tiers": ["commodity"],
        "fallback": NOT_ROUTED,
        "notes": (
            "Needs an OpenAI-compatible endpoint and key configured on the engine before a "
            "stage can use it."
        ),
    },
    # ── Vertex Model Garden, Model-as-a-Service (catalogued, not routed) ──
    {
        "model_id": "llama-3-3-70b-instruct-maas",
        "display_name": "Llama 3.3 70B Instruct (MaaS)",
        "vendor": "meta",
        "availability": "not_enabled",
        "provider_model_name": "meta/llama-3.3-70b-instruct-maas",
        "region": "us-central1",
        "description": (
            "Open-weights option served through Vertex Model-as-a-Service. Structured "
            "output is best-effort; no RTL support."
        ),
        "context_window": 128_000,
        "supports_tools": False,
        "tiers": ["commodity"],
        "fallback": NOT_ROUTED,
        "notes": (
            "Set MODEL_GARDEN_PROJECT_ID on the engine to route it. supports_tools is false: any "
            "stage granting tools would not work on it even once enabled."
        ),
    },
    {
        "model_id": "mistral-small-2503",
        "display_name": "Mistral Small (2503, MaaS)",
        "vendor": "other",
        "availability": "not_enabled",
        "provider_model_name": "mistral-small-2503",
        "region": "us-central1",
        "description": (
            "Mistral's small model through Vertex Model-as-a-Service. 32k-token window, "
            "best-effort structured output."
        ),
        "context_window": 32_000,
        "supports_tools": False,
        "tiers": ["commodity"],
        "fallback": NOT_ROUTED,
        "notes": "Set MODEL_GARDEN_PROJECT_ID on the engine to route it.",
    },
    {
        "model_id": "mistral-medium-3",
        "display_name": "Mistral Medium 3 (MaaS)",
        "vendor": "other",
        "availability": "not_enabled",
        "provider_model_name": "mistral-medium-3",
        "region": "us-central1",
        "description": (
            "Mistral's mid tier through Vertex Model-as-a-Service. Best-effort structured output."
        ),
        "context_window": 128_000,
        "supports_tools": False,
        "tiers": ["portable"],
        "fallback": NOT_ROUTED,
        "notes": "Set MODEL_GARDEN_PROJECT_ID on the engine to route it.",
    },
)

#: Rows an earlier seed wrote that the engine's catalog never had. Marked
#: retired rather than deleted: a stage that referenced one keeps resolving,
#: and the Studio shows why it is no longer selectable.
RETIRED: dict[str, str] = {
    "llama-3-1-70b-instruct-maas": (
        "superseded by llama-3-3-70b-instruct-maas, the id agent-engine's catalog actually carries"
    ),
}


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)

    def record(self, outcome: str, what: str) -> None:
        self.counts[outcome] += 1
        symbols = {"created": "+", "updated": "~", "unchanged": "="}
        print(f"  {symbols.get(outcome, '?')} {outcome:<9} {what}")


def _comparable(row: dict[str, Any]) -> str:
    """Everything except the timestamps, so an unchanged row is recognised as one."""
    return json.dumps(
        {k: v for k, v in sorted(row.items()) if k not in {"created_at", "updated_at"}},
        ensure_ascii=False,
        sort_keys=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        from google.cloud import firestore  # type: ignore[attr-defined]
    except ImportError:
        sys.exit("google-cloud-firestore is not installed in this environment")

    database = ENVIRONMENTS[args.env]
    db = firestore.Client(project=FIRESTORE_PROJECT, database=database)

    print(f"Seeding the model catalog into {FIRESTORE_PROJECT}/{database}")
    print(f"  mode : {'DRY RUN' if args.dry_run else 'WRITING'}")
    print(f"  rows : {len(CATALOG)}\n")

    report = Report()
    now = firestore.SERVER_TIMESTAMP
    for model_id, why in RETIRED.items():
        ref = db.collection(COLLECTION).document(model_id)
        existing = ref.get()
        if not existing.exists or (existing.to_dict() or {}).get("availability") == "retired":
            continue
        if not args.dry_run:
            ref.set({"availability": "retired", "notes": why, "updated_at": now}, merge=True)
        report.record("updated", f"{model_id} (retired: {why})")
    for entry in CATALOG:
        document = {"id": entry["model_id"], **entry}
        document.setdefault("notes", None)
        document.setdefault("description", None)
        document.setdefault("fallback", None)
        label = f"{entry['model_id']} ({entry['availability']})"

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

    print("\n" + "-" * 60)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(report.counts.items()))
    print("summary: " + (summary or "nothing to do"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
