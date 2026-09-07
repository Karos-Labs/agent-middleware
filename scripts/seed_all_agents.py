#!/usr/bin/env python3
"""Register every agent-engine workflow as a first-class control-plane agent.

Six of the eleven were seeded from the karos-agents lab repo, because that is
where their prompts came from. The other five have no lab source, so they
existed only as TypeScript in agent-engine and had no database row at all --
which is why the portal could not list them, price them, or open them in the
Studio.

This writes all eleven with the catalog/Studio contract: name, description,
icon, category, credit cost, the inputs a run dialog should ask for, and the
stages the workflow actually runs.

## Stages are read from code, not written here

``engine_stages.json`` is generated from agent-engine's own workflow sources by
walking every ``wf.step.code``/``.agent``/``.gate`` call. Hand-maintaining a
stage list beside a workflow that changes is how a Studio ends up describing a
program that no longer exists; generating it means the list is either right or
visibly stale.

Retry suffixes are collapsed -- ``05-write-copy-attempt-${attempt}`` is one
stage a workflow may run twice, not two stages.

## What this does NOT touch

Prompts. Those already exist for the six agents with a lab source, and
inventing a v1 for the other five would put words in a client's agent that no
one wrote. They get their row, their metadata and their model; their prompt
stays absent, which the Studio shows as "no prompt" rather than as a default
someone might mistake for reviewed copy.

Idempotent by content comparison: a re-run writes nothing when unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Same bootstrap, and the same reason, as seed_legacy_agents.py: make the
# repository root importable when run as `python scripts/seed_all_agents.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# One source for the readiness table: the report owns it, the descriptor reads
# it. See descriptor_for().
from scripts.report_client_readiness import readiness_paths  # noqa: E402

ENVIRONMENTS: dict[str, str] = {"prep": "prep", "prod": "(default)"}
FIRESTORE_PROJECT = "karoscmo"
COLLECTION = "agents"
STAGES_FILE = Path(__file__).with_name("engine_stages.json")

#: Every hand-written workflow agent-engine can dispatch, with the metadata the
#: portal needs to render it. `model` references the normalized `models`
#: collection (see seed_models.py), never a loose vendor string.
#: Agents that WERE products and no longer are.
#:
#: Removing an entry from ``CATALOG`` is not enough on its own: this script
#: never deletes, so a dropped agent keeps its Firestore document, keeps
#: ``deleted_at: None``, and keeps appearing in the portal catalog exactly as
#: before. Retiring it has to be an action, not an omission.
#:
#: Stamping ``deleted_at`` + ``status: disabled`` is what the control plane's own
#: ``DELETE /agents/{slug}`` does, and ``list_agents`` filters on it. The document
#: is kept rather than deleted for the same reason that endpoint keeps it: runs,
#: prompts and feedback reference the agent, and a dangling id in that history is
#: worse than a retired row.
RETIRED: dict[str, str] = {
    # Inlined into their parent agents as the `00-channel-setup` pre-flight
    # (agent-engine `agents/setup-agents/src/workflow/channel-setup.ts`).
    # Sequencing setup before drafting used to be the operator's job and nothing
    # enforced it; now each drafting agent checks its own channel context.
    "linkedin-setup-agent": "inlined into linkedin-agent as its 00-channel-setup pre-flight",
    "reddit-setup-agent": "inlined into reddit-agent as its 00-channel-setup pre-flight",
}

CATALOG: tuple[dict[str, Any], ...] = (
    {
        "slug": "instagram-agent",
        "name": "Instagram Post / Carousel Creator",
        "description": (
            "Researches a topic, writes a six-to-eight slide carousel, sources and vets imagery, "
            "and renders the slides."
        ),
        "icon": "Camera",
        "category": "social",
        "credit_cost": 12,
        "agent_type": "instagram_carousel",
        "tags": ["social", "instagram", "carousel"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "What should this carousel cover?",
                "required": False,
                "placeholder": "Leave blank to let the agent pick from its topic pool",
            },
        ],
    },
    {
        "slug": "landing-builder-agent",
        "name": "Landing Page Builder",
        "description": (
            "Builds a complete landing page from the client's brand contract and intake, pausing "
            "for human review before delivery."
        ),
        "icon": "Layout",
        "category": "web",
        "credit_cost": 40,
        "agent_type": "landing_page",
        "tags": ["web", "landing-page", "gated"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Anything specific for this build?",
                "required": False,
            },
        ],
    },
    {
        "slug": "x-agent",
        "name": "X / Twitter Content Specialist",
        "description": (
            "Drafts one X post per run against the account's charter, with lane rotation, claim "
            "checks and brand-compliance gates."
        ),
        "icon": "AtSign",
        "category": "social",
        "credit_cost": 6,
        "agent_type": "x_post",
        "tags": ["social", "x", "draft-only"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Topic for this post",
                "required": False,
                "placeholder": "Leave blank to use the lane rotation",
            },
            {
                "key": "requestedLane",
                "type": "select",
                "label": "Lane",
                "required": False,
                "options": ["knowledge", "pov", "product", "engagement"],
            },
        ],
    },
    {
        "slug": "linkedin-agent",
        "name": "LinkedIn Thought Leadership Writer",
        "description": (
            "Drafts one LinkedIn post in the company voice or a named executive's, choosing from "
            "eleven founder archetypes."
        ),
        "icon": "Share2",
        "category": "social",
        "credit_cost": 8,
        "agent_type": "linkedin_post",
        "tags": ["social", "linkedin", "draft-only"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Topic for this post",
                "required": False,
            },
            {
                "key": "requestedIdentityScope",
                "type": "select",
                "label": "Post as",
                "required": False,
                "options": ["company", "executive"],
            },
        ],
    },
    {
        "slug": "reddit-agent",
        "name": "Reddit Community Post Writer",
        "description": (
            "Drafts ONE reply for a chosen thread, against that subreddit's rules. Draft-only by "
            "hard product rule -- a human always posts it."
        ),
        "icon": "MessageSquare",
        "category": "social",
        "credit_cost": 6,
        "agent_type": "reddit_reply",
        "tags": ["social", "reddit", "draft-only"],
        "required_inputs": [
            {
                "key": "requestedSubreddit",
                "type": "text",
                "label": "Subreddit",
                "required": False,
                "placeholder": "r/marketing",
            },
            {"key": "requestedThreadUrl", "type": "text", "label": "Thread URL", "required": False},
        ],
    },
    {
        "slug": "branded-shorts-agent",
        "name": "Branded Shorts Video Creator",
        "description": (
            "Cuts short-form video from a source recording, planning motion graphics from the "
            "transcript rather than from stock."
        ),
        "icon": "Video",
        "category": "video",
        "credit_cost": 45,
        "agent_type": "branded_shorts",
        "tags": ["video", "shorts"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Which recording, and what angle?",
                "required": True,
            },
        ],
    },
    {
        "slug": "intel-report-agent",
        "name": "Competitive Intelligence & Market Analysis",
        "description": (
            "Pulls competitive and market research for a client and writes it up as a structured "
            "intelligence report."
        ),
        "icon": "Search",
        "category": "research",
        "credit_cost": 25,
        "agent_type": "intel_report",
        "tags": ["research", "intel"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "What should this report focus on?",
                "required": False,
            },
        ],
    },
    {
        "slug": "blog-agent",
        "name": "SEO & Long-form Blog Writer",
        "description": (
            "Researches and writes a long-form article with an SEO structure, internal linking and "
            "a self-critique pass."
        ),
        "icon": "FileText",
        "category": "content",
        "credit_cost": 20,
        "agent_type": "blog_article",
        "tags": ["content", "blog", "seo"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Article topic or brief",
                "required": False,
            },
        ],
    },
    {
        "slug": "newsletter-agent",
        "name": "Email Newsletter & Campaign Writer",
        "description": (
            "Writes one newsletter issue from the client's recent material, with a compliance pass "
            "before delivery."
        ),
        "icon": "Mail",
        "category": "content",
        "credit_cost": 18,
        "agent_type": "newsletter_issue",
        "tags": ["content", "newsletter", "email"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Theme for this issue",
                "required": False,
            },
        ],
    },
    {
        "slug": "reputation-agent",
        "name": "Reputation Management & Review Responder",
        "description": (
            "Mines response behaviour, assesses reputation state and drafts replies. Publishing is "
            "permanently gated inside the workflow."
        ),
        "icon": "Shield",
        "category": "reputation",
        "credit_cost": 15,
        "agent_type": "reputation",
        "tags": ["reputation", "draft-only", "gated"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Anything to focus on?",
                "required": False,
            },
        ],
    },
    {
        "slug": "seo-geo-agent",
        "name": "Local SEO & Geo-targeted Content Specialist",
        "description": (
            "Captures search and answer-engine visibility for a client, finds the gaps and drafts "
            "the fixes."
        ),
        "icon": "MapPin",
        "category": "seo",
        "credit_cost": 22,
        "agent_type": "seo_geo",
        "tags": ["seo", "geo", "research"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "Which locations or queries?",
                "required": False,
            },
        ],
    },
    {
        # In KNOWN_PRODUCT_IDS with fifteen recorded stages, and it had no
        # catalog row -- so it was dispatchable by the engine and invisible to
        # the portal, which also meant it could not have a C4 descriptor.
        #
        # `is_public: False` is the conservative half of a product decision
        # nobody has made: whether a client should be able to ask for a whole
        # campaign in one request, or whether this stays an internal
        # composition step the staff drive. False routes it for staff and
        # hides it from client surfaces; flipping it is one field.
        "slug": "campaign-orchestrator",
        "name": "Campaign Orchestrator",
        "description": (
            "Plans a multi-channel campaign and composes the per-channel briefs the "
            "drafting agents run from."
        ),
        "icon": "CalendarRange",
        "category": "orchestration",
        "credit_cost": 30,
        "agent_type": "campaign",
        "is_public": False,
        "tags": ["orchestration", "campaign", "gated"],
        "required_inputs": [
            {
                "key": "request",
                "type": "textarea",
                "label": "What is this campaign for?",
                "required": True,
                "placeholder": "The launch, the season, the announcement",
            },
        ],
    },
    {
        "slug": "tiktok-agent",
        "name": "TikTok Commentary Clips",
        "description": (
            "Finds the single best moment in a long-form episode, cuts it on sentence boundaries, "
            "and writes the client's own take over it. Draft-only: a human approves every clip."
        ),
        "icon": "Video",
        "category": "social",
        "credit_cost": 12,
        "agent_type": "tiktok_clip",
        "tags": ["social", "tiktok", "video", "draft-only"],
        "required_inputs": [
            {
                "key": "sourcePath",
                "type": "text",
                "label": "Episode media file",
                "required": True,
                "placeholder": "The long-form episode this clip comes out of",
            },
            {
                "key": "request",
                "type": "textarea",
                "label": "A specific moment to clip?",
                "required": False,
                "placeholder": "Leave blank to take the next candidate from the topic catalog",
            },
        ],
    },
)

#: Default model for every agent, by normalized id from the `models`
#: collection. One default rather than per-agent guesses: nothing here has a
#: measured reason to differ yet, and a fabricated per-agent split would read
#: as a decision somebody made.
DEFAULT_MODEL_ID = "claude-sonnet-4-6-on-vertex"

#: Human labels for stage-id prefixes, so the Studio reads as steps rather than
#: as slugs. Anything unmatched falls back to the id with hyphens removed.
STAGE_LABEL_HINTS: tuple[tuple[str, str], ...] = (
    ("intake", "Intake check"),
    ("load-client-context", "Load client context"),
    ("load-memory-shelf", "Load memory shelf"),
    ("research", "Research pull"),
    ("draft", "Draft"),
    ("copy", "Write copy"),
    ("render", "Render"),
    ("verify", "Verify"),
    ("gate", "Gate"),
    ("review", "Human review"),
    ("deliver", "Deliver"),
    ("persist", "Persist deliverable"),
    ("commit", "Commit and record"),
    ("upload", "Upload"),
)


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)

    def record(self, outcome: str, what: str) -> None:
        self.counts[outcome] += 1
        symbols = {"created": "+", "updated": "~", "unchanged": "=", "retired": "-"}
        print(f"  {symbols.get(outcome, '?')} {outcome:<9} {what}")


def humanize(stage_id: str) -> str:
    """A readable label for a step id, without inventing meaning it lacks."""
    body = stage_id.split("-", 1)[1] if "-" in stage_id and stage_id[0].isdigit() else stage_id
    for needle, label in STAGE_LABEL_HINTS:
        if needle in body:
            return label
    return body.replace("-", " ").strip().capitalize() or stage_id


def load_stages() -> dict[str, list[dict[str, Any]]]:
    if not STAGES_FILE.is_file():
        sys.exit(
            f"{STAGES_FILE.name} is missing. Regenerate it from agent-engine's workflow sources "
            "(see this script's docstring) rather than hand-writing a stage list."
        )
    raw = json.loads(STAGES_FILE.read_text(encoding="utf-8"))
    return {
        slug: [
            {
                "id": step["id"],
                "label": humanize(step["id"]),
                "description": None,
                # A gate pauses for a human, which is the difference between an
                # agent that finishes on its own and one that waits.
                "is_gate": step["kind"] == "gate",
                # Carried through, not just collapsed into `is_gate`. Only an
                # "ai" stage calls a model, and the Studio offers its per-stage
                # model picker on exactly those -- so dropping this field made
                # every stage read as "code" and the picker appear nowhere.
                "kind": step["kind"],
                # Which prompt this stage loads, when it loads one. The Studio
                # needs it to put the right prompt editor beside the right
                # stage; the engine resolves it from the agent class's own
                # config, so this is a mirror of a fact rather than a source
                # of one. Absent on code steps and on the shared guardrail,
                # which builds its prompt inline from client config.
                "skill_ref": step.get("skill_ref"),
                # No override until somebody sets one in the Studio. Written
                # explicitly so the field exists on the document rather than
                # being absent and read as a default.
                "model_id": None,
            }
            for step in steps
        ]
        for slug, steps in raw.items()
    }


# --- C4 capability descriptor ------------------------------------------------
#
# docs/contracts/C4-capability-descriptor.md. The half that cannot be derived
# from a workflow's source, because it says what an agent is FOR rather than
# what it does step by step. Written here once; the portal consumes it and
# never hand-writes it.
#
# Two fields are DELIBERATELY absent from every row and set from code below,
# because they are derived and a hand-written copy would go stale:
# `gates` comes from the generated stages, and `readiness` from
# report_client_readiness.py's own table.

#: `supports_target_date` is false everywhere, and that is a finding rather
#: than an omission: `targetDate` appears NOWHERE in agent-engine. C3 defines
#: it and T-A13 is the ticket that makes agents read it. Setting true here
#: would put a field on the descriptor that says yes and does nothing -- which
#: is the exact failure C3's own first principle forbids ("no displayed field
#: that is not read"). Each row flips as its agent learns to read the date.
_TARGET_DATE_UNIMPLEMENTED = False

#: Only three agents read `mediaAssets` -- verified by grep across
#: agents/*/src, not assumed from the product's name. landing-builder-agent is
#: false on purpose: C3 routes its `references` INTO mediaAssets, and that work
#: has not landed, so today an upload handed to it goes nowhere.
DESCRIPTORS: dict[str, dict[str, Any]] = {
    "x-agent": {
        "capabilities": ["draft_social_post"],
        "platforms": ["x"],
        "consumes_media": False,
        "custom_agent_keys": ["karos-x-agent-v2"],
    },
    "linkedin-agent": {
        # `run_setup` as well as drafting: the setup workflow was inlined as
        # this agent's `00-channel-setup` pre-flight, so a run dispatched from
        # the lab's setup key carries the same filled form it always did and
        # this agent records it. The capability did not disappear when the
        # separate product did.
        "capabilities": ["draft_social_post", "run_setup"],
        "platforms": ["linkedin"],
        "consumes_media": False,
        "custom_agent_keys": ["karos-linkedin-writer-v2", "karos-linkedin-setup-v2"],
    },
    "reddit-agent": {
        "capabilities": ["draft_reply", "run_setup"],
        "platforms": ["reddit"],
        "consumes_media": False,
        "custom_agent_keys": ["karos-reddit-runner", "karos-reddit-setup"],
    },
    "instagram-agent": {
        "capabilities": ["draft_social_post", "produce_carousel"],
        "platforms": ["instagram"],
        "consumes_media": True,
        "custom_agent_keys": ["karos-instagram-agent"],
    },
    "tiktok-agent": {
        # Its own product, not branded-shorts under another name: this finds a
        # moment inside someone else's long-form episode and puts the client's
        # commentary on it.
        "capabilities": ["draft_social_post", "produce_video"],
        "platforms": ["tiktok"],
        "consumes_media": True,
        "custom_agent_keys": ["karos-tiktok-agent"],
    },
    "branded-shorts-agent": {
        # Turns ONE uploaded talking-head video into one vertical short. The
        # short itself is platform-agnostic; these are the two surfaces the
        # portal publishes it to. THE ONE ROW I WOULD HAVE PRODUCT CONFIRM.
        "capabilities": ["produce_video"],
        "platforms": ["tiktok", "instagram"],
        "consumes_media": True,
        "custom_agent_keys": ["branded-shorts"],
    },
    "blog-agent": {
        "capabilities": ["draft_article"],
        "platforms": ["web"],
        "consumes_media": False,
        "custom_agent_keys": ["karos-blog-writer-v2"],
    },
    "newsletter-agent": {
        "capabilities": ["draft_newsletter"],
        "platforms": ["email"],
        "consumes_media": False,
        "custom_agent_keys": ["karos-newsletter-writer-v2"],
    },
    "landing-builder-agent": {
        "capabilities": ["build_landing_page"],
        "platforms": ["web"],
        "consumes_media": False,
        "custom_agent_keys": ["landing-builder"],
    },
    "seo-geo-agent": {
        # No platforms: an audit is not published to a channel. An empty list
        # is a real answer here, and the router treats a platform request
        # against it as a mismatch rather than a wildcard.
        "capabilities": ["run_seo_audit"],
        "platforms": [],
        "consumes_media": False,
        "custom_agent_keys": ["seo-geo-agent-v2"],
    },
    "intel-report-agent": {
        "capabilities": ["run_intel_report"],
        "platforms": [],
        "consumes_media": False,
        "custom_agent_keys": [],
    },
    "reputation-agent": {
        # It drafts replies to reviews -- same capability as reddit-agent, a
        # different surface. `platforms` is empty because the reply goes back to
        # whichever review site the review came from, which is per-run data and
        # not a property of the agent.
        "capabilities": ["draft_reply"],
        "platforms": [],
        "consumes_media": False,
        "custom_agent_keys": ["karos-reputation-runner"],
    },
    "campaign-orchestrator": {
        "capabilities": ["orchestrate_campaign"],
        "platforms": [],
        "consumes_media": False,
        "custom_agent_keys": [],
    },
}

#: The five portal agents with no agent-engine workflow behind them.
#:
#: They get a row so the chat router can resolve their key to a descriptor that
#: says `legacy_only` and answer "that is still on the old path". Without one,
#: C4 invariant 2 -- an agent with no descriptor is not routable, no fallback to
#: the name -- would make them invisible rather than explained, and the router
#: would be back to matching strings.
#:
#: `capabilities` is deliberately empty on all five: `legacy_only` is not a
#: routable state, so advertising an ability the engine cannot honour would
#: invite exactly the routing this status exists to prevent. The slug is the
#: portal key, because there is no engine productId to borrow.
LEGACY_ONLY_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "slug": "karos-carousel-runner",
        "name": "Carousel Runner (legacy)",
        "description": "Carousel generation on the agent-service path. No agent-engine workflow.",
        "category": "social",
        "tags": ["legacy", "carousel"],
    },
    {
        "slug": "karos-carousel-setup",
        "name": "Carousel Setup (legacy)",
        "description": "Carousel onboarding on the agent-service path. No agent-engine workflow.",
        "category": "social",
        "tags": ["legacy", "carousel"],
    },
    {
        "slug": "karos-carousel-manager",
        "name": "Carousel Manager (legacy)",
        "description": "Carousel scheduling on the agent-service path. No agent-engine workflow.",
        "category": "social",
        "tags": ["legacy", "carousel"],
    },
    {
        "slug": "karos-linkedin-manager-v2",
        "name": "LinkedIn Manager (legacy)",
        # The documented case, and the reason this status exists rather than a
        # missing row: it runs on two clocks and rewrites the generators'
        # inputs, and agent-engine has neither a scheduler nor a write path for
        # that. It is not waiting on a migration nobody did.
        "description": (
            "Runs on two clocks and rewrites the generators' inputs. agent-engine has "
            "neither a scheduler nor a write path for that, so it stays on agent-service."
        ),
        "category": "social",
        "tags": ["legacy", "linkedin"],
    },
    {
        "slug": "karos-reputation-manager",
        "name": "Reputation Manager (legacy)",
        "description": "Reputation scheduling on the agent-service path. No agent-engine workflow.",
        "category": "reputation",
        "tags": ["legacy", "reputation"],
    },
)


def descriptor_for(slug: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
    """The C4 descriptor fields for one agent: written, plus derived.

    `gates` is read off the stages the generator produced rather than listed by
    hand, so it cannot disagree with the workflow. It was empty for every agent
    until the generator learned the two ways a gate is actually declared --
    through the shared review-cycle primitive, and inside a ternary whose other
    arm is an autoApprove `code` step with the same id.

    `readiness` comes from report_client_readiness.py's own table, for the same
    reason: one source, and the copy that would have drifted is the one a
    planner uses to tell a client whether they can have a post today.
    """

    written = DESCRIPTORS.get(slug, {})
    gates = [s["gate_kind"] for s in stages if s.get("is_gate") and s.get("gate_kind")]
    try:
        hard, soft = readiness_paths(slug)
    except KeyError:
        # A product the readiness report does not score yet, e.g.
        # campaign-orchestrator. Empty is honest; inventing requirements is not.
        hard, soft = [], []
    return {
        "capabilities": list(written.get("capabilities", [])),
        "platforms": list(written.get("platforms", [])),
        "consumes_media": bool(written.get("consumes_media", False)),
        "supports_target_date": _TARGET_DATE_UNIMPLEMENTED,
        "custom_agent_keys": list(written.get("custom_agent_keys", [])),
        # De-duplicated, order preserved: one agent can hold two gates of the
        # same kind across revision rounds and the descriptor lists kinds.
        "gates": list(dict.fromkeys(gates)),
        "readiness": {"hard": hard, "soft": soft},
    }


def build_document(entry: dict[str, Any], stages: list[dict[str, Any]], now: Any) -> dict[str, Any]:
    return {
        "id": entry["slug"],
        "slug": entry["slug"],
        "name": entry["name"],
        "description": entry["description"],
        "status": "active",
        "agent_type": entry["agent_type"],
        "model": DEFAULT_MODEL_ID,
        "model_params": {},
        "config": {},
        "tags": entry["tags"],
        "icon": entry["icon"],
        "category": entry["category"],
        "credit_cost": entry["credit_cost"],
        # Per entry, defaulting to public: every drafting agent is, and the one
        # that is not says so in its own row with the reason.
        "is_public": entry.get("is_public", True),
        "required_inputs": [
            {
                "key": i["key"],
                "type": i.get("type", "text"),
                "label": i["label"],
                "help_text": i.get("help_text"),
                "required": i.get("required", False),
                "placeholder": i.get("placeholder"),
                "options": i.get("options", []),
            }
            for i in entry["required_inputs"]
        ],
        "stages": stages,
        # These stages are TypeScript. Recorded so the Studio can show what an
        # agent does; editing the list would change a page and not a program.
        "stages_read_only": True,
        **descriptor_for(entry["slug"], stages),
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }


def build_legacy_document(entry: dict[str, Any], now: Any) -> dict[str, Any]:
    """A row for a portal agent with no agent-engine workflow.

    Everything a descriptor consumer reads is present and empty rather than
    absent, so the router gets a definite "nothing here" instead of a missing
    field it has to interpret. `status: legacy_only` is the whole payload of
    information; `capabilities: []` makes sure it cannot be routed to by
    accident even if something ignores the status.
    """

    return {
        "id": entry["slug"],
        "slug": entry["slug"],
        "name": entry["name"],
        "description": entry["description"],
        "status": "legacy_only",
        "agent_type": None,
        "model": None,
        "model_params": {},
        "config": {},
        "tags": entry["tags"],
        "icon": entry.get("icon"),
        "category": entry["category"],
        "credit_cost": None,
        # Not client-facing: the portal already renders these from its own
        # customAgents; the row exists so the CHAT ROUTER can resolve the key.
        "is_public": False,
        "required_inputs": [],
        "stages": [],
        # No stages to be read-only about. False rather than True so nothing
        # reads it as "compiled code we cannot show you".
        "stages_read_only": False,
        "capabilities": [],
        "platforms": [],
        "consumes_media": False,
        "supports_target_date": False,
        "custom_agent_keys": [entry["slug"]],
        "gates": [],
        "readiness": {"hard": [], "soft": []},
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }


def preserve_stage_models(
    current: Any, incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Carries a Studio-set ``model_id`` across a re-seed.

    ``stages`` is regenerated wholesale from ``engine_stages.json``, because the
    stage LIST is the workflow's and this script is its mirror. ``model_id`` is
    the one field on a stage that a person sets rather than the workflow, so a
    re-seed that rebuilt the list without it would silently reset every
    per-stage model choice -- the same shape of bug as a deploy dropping an
    env var, and just as quiet.

    Matched on stage id. A stage that no longer exists loses its override,
    which is correct: there is nothing left for it to configure.
    """
    if not isinstance(current, list):
        return incoming
    chosen = {
        stage.get("id"): stage.get("model_id")
        for stage in current
        if isinstance(stage, dict) and stage.get("model_id")
    }
    if not chosen:
        return incoming
    return [
        {**stage, "model_id": chosen.get(stage["id"], stage.get("model_id"))}
        for stage in incoming
    ]


def comparable(row: dict[str, Any]) -> str:
    return json.dumps(
        {k: v for k, v in sorted(row.items()) if k not in {"created_at", "updated_at"}},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
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
    stages_by_slug = load_stages()

    print(f"Registering agent-engine workflows in {FIRESTORE_PROJECT}/{database}")
    print(f"  mode   : {'DRY RUN' if args.dry_run else 'WRITING'}")
    print(f"  agents : {len(CATALOG)} live, {len(RETIRED)} retired\n")

    report = Report()
    now = firestore.SERVER_TIMESTAMP

    for slug, why in RETIRED.items():
        ref = db.collection(COLLECTION).document(slug)
        existing = ref.get()
        if not existing.exists:
            continue
        current = existing.to_dict() or {}
        if current.get("deleted_at") is not None:
            report.record("unchanged", f"{slug} (already retired)")
            continue
        if args.dry_run:
            report.record("retired", f"{slug} -- {why}")
            continue
        # Same shape as DELETE /agents/{slug}: stamped and disabled, never
        # removed. ``retired_reason`` is ours -- the endpoint records no why, and
        # a row that vanished from the catalog with no explanation is the thing
        # somebody re-adds by accident six months later.
        ref.set(
            {"deleted_at": now, "status": "disabled", "retired_reason": why, "updated_at": now},
            merge=True,
        )
        report.record("retired", f"{slug} -- {why}")

    for entry in CATALOG:
        slug = entry["slug"]
        stages = stages_by_slug.get(slug)
        if stages is None:
            # A workflow the extractor did not see is a real mismatch between
            # this catalog and the engine, not something to paper over.
            sys.exit(f"no stages recorded for '{slug}' — regenerate engine_stages.json")

        document = build_document(entry, stages, now)
        label = f"{slug} ({len(stages)} stages, {entry['credit_cost']} credits)"

        if args.dry_run:
            report.record("created", label)
            continue

        ref = db.collection(COLLECTION).document(slug)
        existing = ref.get()
        if existing.exists:
            current = existing.to_dict() or {}
            document = {
                **document,
                "stages": preserve_stage_models(current.get("stages"), document["stages"]),
            }
            merged = {**current, **document}
            if comparable({**current, "id": slug}) == comparable(merged):
                report.record("unchanged", label)
                continue
            # merge=True: an agent seeded from the lab repo already has a
            # prompt subcollection and fields this script does not own, and a
            # full overwrite would quietly drop them.
            ref.set(
                {**document, "created_at": current.get("created_at", now), "updated_at": now},
                merge=True,
            )
            report.record("updated", label)
            continue

        ref.set(document)
        report.record("created", label)

    for entry in LEGACY_ONLY_AGENTS:
        slug = entry["slug"]
        document = build_legacy_document(entry, now)
        label = f"{slug} (legacy_only)"
        if args.dry_run:
            report.record("created", label)
            continue
        ref = db.collection(COLLECTION).document(slug)
        existing = ref.get()
        if existing.exists:
            current = existing.to_dict() or {}
            merged = {**current, **document}
            if comparable({**current, "id": slug}) == comparable(merged):
                report.record("unchanged", label)
                continue
            ref.set(
                {**document, "created_at": current.get("created_at", now), "updated_at": now},
                merge=True,
            )
            report.record("updated", label)
            continue
        ref.set(document)
        report.record("created", label)

    print("\n" + "-" * 60)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(report.counts.items()))
    print("summary: " + (summary or "nothing to do"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
