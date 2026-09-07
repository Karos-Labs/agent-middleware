#!/usr/bin/env python3
"""Project karosCMO's client records into the agent-engine workspace store.

agent-engine reads a client's onboarding data through the ``client.*`` tools,
which read one JSON record per key out of the workspace bucket::

    gs://<bucket>/clients/<slug>/client/profile.json
    gs://<bucket>/clients/<slug>/client/brand.json
    gs://<bucket>/clients/<slug>/client/voice-rules.json
    gs://<bucket>/clients/<slug>/client/config.json
    gs://<bucket>/clients/<slug>/client/competitors.json
    gs://<bucket>/clients/<slug>/context/<docType>.json

Production had none of these, so every production agent-engine run would stop
at ``blocked_intake`` before doing any work. This projects them from the three
places the data actually lives:

* **karosCMO Firestore** (``clients`` collection) -- the authoritative record
  a human edits in the portal: name, industry, website, branding guidelines,
  brand voice. Source for profile / brand / voice-rules.
* **an existing environment's ``config.json``** (``--carry-config-from``) --
  engine-specific settings that are not portal fields and cannot be derived
  from one: an X handle, a client's frozen Instagram style config. Carried
  forward rather than regenerated.

Nothing is invented on the default path. A client whose record has no industry
gets no ``industry`` key, not a guess. In particular this does NOT synthesise
``instagramStyleConfig``/``instagramBrandTokens``: instagram-agent refuses to
guess those and blocks the run instead, and a seeder that quietly invented
them would defeat exactly the check that exists to stop unreviewed styling
reaching a client's feed.

The last two paths are the C1 contract (``docs/contracts/C1-client-context.md``)
and they are the richer half. ``client/profile|brand|voice-rules`` are a handful
of fields off the client record -- one free-text voice line and some colours --
while ``context/<docType>.json`` carries the analyst-grade documents the
onboarding pipeline actually writes: brand voice, market strategy, competitor
analysis, product information, branding guidelines, target audience, and the
three per-agent identity profiles. Each is projected with full provenance
(which Firestore document, at which version, when, by which mechanism, and a
hash of the text) so ``report_client_readiness.py`` can say how stale the copy
an agent reads has become, rather than assuming it is current.

Only the ``internal`` tier is projected, and never with a fallback -- see
``PROJECTED_TIER``. And ``clientCompetitors`` fills the one path
``client.listCompetitors`` has always read and nothing has ever written.

``--skeleton`` is the exception, and it is refused against production for that
reason. It writes prep-only placeholders for everything an agent refuses to
start without, plus one record that is not per-agent at all::

    gs://<bucket>/clients/<slug>/topics/catalog.json

the no-repeat topic catalog every channel agent reserves a subject from.
NOTHING IN EITHER REPO EVER WRITES THAT FILE — ``topics.topUp`` has one
production caller (``topics.reserve``'s own proactive top-up) and it passes an
empty list — so it stays absent, reads as zero available rows, and every
``topics.reserve`` call breaches the lane floor forever. See
``skeleton_topics_catalog``.

Idempotent by content hash for the ``client/*`` records, and create-only for
the ``--skeleton`` extras: a record already present is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prep": {"bucket": "karoscmo-prep-agent-artifacts", "database": "prep"},
    "prod": {"bucket": "karoscmo-prod-agent-artifacts", "database": "(default)"},
}

FIRESTORE_PROJECT = "karoscmo"

#: Named rather than written inline so the skeleton markdown below stays a list
#: of readable lines instead of one string full of escapes.
LINE_BREAK = "\n"

#: Slugs that are test fixtures, never real clients. Copying these into a real
#: environment is how synthetic data ends up in a production listing.
TEST_SLUGS = {"acme", "acme2", "acme3", "acme4", "acme-corp", "test", "demo"}


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)

    def record(self, outcome: str, what: str) -> None:
        self.counts[outcome] += 1
        symbols = {"created": "+", "updated": "~", "unchanged": "=", "skipped": "-"}
        print(f"  {symbols.get(outcome, '?')} {outcome:<9} {what}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so an absent field stays absent rather than becoming ''."""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def build_profile(doc: dict[str, Any]) -> dict[str, Any]:
    return _clean(
        {
            "name": doc.get("name"),
            "slug": doc.get("agentsRepoSlug"),
            "industry": doc.get("industry"),
            "description": doc.get("description"),
            "website": doc.get("website"),
            "domains": doc.get("domains"),
        }
    )


def build_brand(doc: dict[str, Any]) -> dict[str, Any]:
    guidelines = doc.get("brandingGuidelines") or {}
    colors = _clean(
        {
            "primaryAccent": guidelines.get("primaryAccent") or doc.get("accentColor"),
            "secondaryAccent": guidelines.get("secondaryAccent"),
            "neutralDark": guidelines.get("brandNeutralDark"),
            "neutralLight": guidelines.get("brandNeutralLight"),
        }
    )
    return _clean(
        {
            "name": doc.get("name"),
            "accent": guidelines.get("primaryAccent") or doc.get("accentColor"),
            "colors": colors,
            "dominantColors": guidelines.get("dominantColors"),
            "fonts": _clean(
                {"heading": guidelines.get("fontHeading"), "body": guidelines.get("fontBody")}
            ),
            "visualStyle": guidelines.get("visualStyle"),
            "guidelines": guidelines.get("guidelines"),
            "logoUrl": guidelines.get("logoUrl") or doc.get("logoUrl"),
            # The client's own Instagram handle, rendered as the @-watermark
            # on every slide by instagram-agent's Brand Kit. Normalisation
            # (single leading "@", character whitelist) happens on the engine
            # side (deriveBrandRenderTokens), so this projects the portal
            # value verbatim.
            "handle": _instagram_handle(doc),
        }
    )


def _instagram_handle(doc: dict[str, Any]) -> str | None:
    """The instagram handle from karosCMO's ``socialLinks``, tolerant of a full URL.

    Portal users paste either a bare handle ("geektimecoil", "@geektimecoil")
    or a profile URL ("https://instagram.com/geektimecoil/"); the engine wants
    the bare name and drops anything it cannot sanitise, so a malformed value
    costs the watermark, never the run.
    """
    social = doc.get("socialLinks") or {}
    raw = social.get("instagram")
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip().rstrip("/")
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    value = value.lstrip("@").split("?")[0]
    return value or None


def build_voice_rules(doc: dict[str, Any]) -> dict[str, Any]:
    guidelines = doc.get("brandingGuidelines") or {}
    tone_keywords = guidelines.get("toneKeywords") or []
    return _clean(
        {
            # brandVoice is the portal's own free-text voice field; the tone
            # keywords are the structured half. Both are the client's words.
            "tone": doc.get("brandVoice") or (", ".join(tone_keywords) if tone_keywords else None),
            "toneKeywords": tone_keywords,
            # Deliberately not populated from anything: forbiddenTerms is a
            # real editorial gate and there is no portal field for it yet.
            # An empty list here would read as "nothing is forbidden".
            "guidelines": guidelines.get("guidelines"),
        }
    )


# --- Context documents (C1) -------------------------------------------------
#
# The envelope and its three builders now live in
# ``app/services/client_context.py``, because the re-seed endpoint (S-A15) has
# to produce byte-identical records and two implementations of one wire format
# drift silently: an agent would be grounded on whichever path ran last, with
# nothing to say which. Re-exported here so this script's own public surface --
# and the tests written against it -- are unchanged.
from app.services.client_context import (  # noqa: E402
    PROJECTED_DOC_TYPES,
    PROJECTED_TIER,
    build_competitors,
    build_context_record,
    competitors_path,
    context_path,
    context_record_is_current,
)

__all__ = [
    "PROJECTED_DOC_TYPES",
    "PROJECTED_TIER",
    "build_competitors",
    "build_context_record",
    "competitors_path",
    "context_path",
    "context_record_is_current",
]


#: Every field an agent-engine workflow refuses to start without, gathered from
#: each workflow's own `WorkflowBlockedIntake` checks.
#:
#: This is invented configuration for real client records, which is why it is
#: refused outright against production below. In prep it is what lets all
#: eleven agents actually run; in production these are decisions a human makes
#: per client, and a placeholder would be indistinguishable from one.
#:
#: instagram-agent deliberately "refuses to guess defaults" for its style
#: config. Supplying one here does not weaken that: the refusal exists so
#: unreviewed styling cannot reach a client's feed, and prep has no feed.
def skeleton_config(doc: dict[str, Any], slug: str) -> dict[str, Any]:
    guidelines = doc.get("brandingGuidelines") or {}
    accent = guidelines.get("primaryAccent") or doc.get("accentColor") or "#ff6b2c"
    industry = doc.get("industry") or "this industry"

    return {
        # Marked in the document itself, not only in this file: whoever opens
        # this in a console should see immediately that nobody chose it.
        "_placeholder": True,
        "_note": (
            "Prep-only skeleton written by seed_client_context.py --skeleton so every agent "
            "can run without blocked_intake. Not real client configuration; never seed this "
            "into production."
        ),
        # x-agent
        "xHandle": slug,
        # reddit-agent
        "targetSubreddits": ["r/test"],
        # blog-agent
        "targetKeywords": [industry],
        "contentPillars": [industry],
        # newsletter-agent
        "targetAudience": f"{industry} buyers",
        "frequency": "weekly",
        # instagram-agent — structural canvas values plus the client's real accent
        "instagramStyleConfig": {
            "style_config_version": 1,
            "canvas": {"w": 1080, "h": 1440, "scale": 2, "slides_min": 6, "slides_max": 8},
            "rules": [],
            "banned_words": [],
            "banned_chars": [],
            "compliance": {"regulated": False, "required_framing": [], "never_say": []},
        },
        "instagramBrandTokens": {
            "templateDir": "agents/instagram-agent/assets/templates/default",
            "slideTemplate": "slide.html",
            "accentColor": accent,
        },
        # branded-shorts-agent
        "brandedShortsProfilePath": f"clients/{slug}/brand/profile.md",
        "brandedShortsGraphicsLanguage": "clean lower-thirds, no stock footage",
        "brandedShortsApprovedArchetypes": ["lower-third", "full-frame-quote"],
    }


#: ``LANE_FLOOR`` in agent-engine's ``packages/tools/karos-topics/src/reserve.ts``.
#: ``topics.reserve`` refuses a reservation that would leave a lane below this,
#: so a lane needs FLOOR + 1 rows to serve even one run.
TOPIC_LANE_FLOOR = 5

#: ``DEFAULT_CAROUSEL_LANE`` in instagram-agent's workflow, and ``DEFAULT_LANE``
#: in karos-topics' catalog module. instagram-agent is the only caller that
#: passes a lane at all; every other agent reads the catalog lane-agnostically,
#: so rows seeded here serve all of them.
TOPIC_DEFAULT_LANE = "general"

#: Subject PROMPTS, not claims — each one names something to write about and
#: asserts nothing about the client. That distinction is the whole reason this is
#: seedable at all: a fabricated statistic or a made-up milestone would be the
#: kind of invention this script refuses everywhere else, while "how {industry}
#: teams evaluate new tooling" is a brief the drafting agent then has to research
#: and source for itself.
#:
#: Two weeks of daily cadence plus the floor, which is what carousel-agent-v2's
#: SKILL.md step 04 asks for ("at least two weeks of cadence per lane, floor 5
#: unused rows per lane") and what makes the difference between a catalog that
#: can serve runs and one that breaches on the first.
TOPIC_SUBJECT_TEMPLATES: tuple[str, ...] = (
    "what changed in {industry} this quarter",
    "how {industry} teams evaluate new tooling",
    "the most common {industry} onboarding mistake",
    "what buyers ask before signing in {industry}",
    "a workflow we would rebuild from scratch",
    "the metric {industry} teams over-index on",
    "why {industry} pilots stall after month one",
    "what a good {industry} brief actually contains",
    "the difference between busy and effective in {industry}",
    "how to tell a real {industry} bottleneck from a symptom",
    "what we look for when reviewing {industry} output",
    "the case for fewer, better {industry} deliverables",
    "what {industry} teams get wrong about automation",
    "how we decide what not to build",
    "the handoff that breaks most {industry} projects",
    "questions worth asking before a {industry} rebuild",
    "what a first {industry} engagement should cover",
    "signals a {industry} process needs rethinking",
    "how small teams compete in {industry}",
    "what we changed after a {industry} project went sideways",
)


def skeleton_topics_catalog(doc: dict[str, Any], slug: str) -> list[dict[str, Any]]:
    """A seeded ``topics/catalog.json`` so the no-repeat catalog can serve runs.

    NOTHING IN EITHER REPO EVER POPULATES THIS FILE. ``topics.topUp`` — the one
    tool that appends rows — has exactly one production caller, ``topics.reserve``
    itself, and it calls it with an empty topics array: a proactive top-up that
    is a documented no-op until an "invent evidenced candidates" capability
    exists. So a client's catalog stays absent forever unless something writes
    it, and an absent catalog reads as zero available rows.

    That was survivable for the agents that treat a reserve ``content_fail`` as
    "the catalog can't help this run" and fall through to a research-derived
    subject. It was fatal for instagram-agent, which used to throw
    ``WorkflowHeld`` on a breach — so on prep, where no catalog had ever been
    written, every single instagram run died at step 03 with "topics catalog
    floor breached". instagram-agent now falls back like its siblings, and this
    seed removes the underlying gap rather than only its worst symptom: with a
    real catalog the dedup gate is doing its actual job (no repeats across runs)
    instead of being permanently bypassed.

    Marked ``_placeholder`` PER ROW, because the catalog is a JSON array with no
    envelope to hang one marker on. The engine reads only
    ``topic``/``normalized``/``status``/``lane``/``reservationKey``, so the extra
    key is inert, survives the read-modify-write ``topics.reserve`` does, and is
    visible to anyone who opens the file.
    """
    industry = doc.get("industry") or "our industry"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for template in TOPIC_SUBJECT_TEMPLATES:
        topic = template.format(industry=industry)
        normalized = topic.strip().lower()
        # `topics.reserve` dedups on `normalized`; two templates collapsing onto
        # one string after substitution would seed a row that can never be
        # reserved separately.
        if normalized in seen:
            continue
        seen.add(normalized)
        rows.append(
            {
                "_placeholder": True,
                "topic": topic.strip(),
                "normalized": normalized,
                "status": "available",
                "lane": TOPIC_DEFAULT_LANE,
            }
        )
    if len(rows) <= TOPIC_LANE_FLOOR:
        # Never write a catalog that cannot serve a single reservation — that is
        # indistinguishable from the empty-catalog state this exists to fix.
        raise AssertionError(
            f"{slug}: seeded topic catalog has {len(rows)} rows, at or below the "
            f"floor of {TOPIC_LANE_FLOOR}; topics.reserve would breach immediately"
        )
    return rows


#: Three agents need more than `client/config.json`, and one shared record does.
#:
#: landing-builder-agent reads an assembled input bundle (a brand contract plus
#: an intake brief), and branded-shorts-agent refuses to run until Style
#: Exploration has left a locked style in the client's memory shelf. Both are
#: real onboarding outputs in production; in prep they are the difference
#: between "every agent runs" and "nine of eleven do".
#:
#: `topics/catalog` is the shared one — the no-repeat catalog every channel
#: agent reserves from, and the one record NOTHING in either repo ever writes.
#: See `skeleton_topics_catalog`.
def skeleton_extras(doc: dict[str, Any], slug: str) -> dict[str, tuple[str, Any]]:
    guidelines = doc.get("brandingGuidelines") or {}
    name = doc.get("name") or slug
    accent = guidelines.get("primaryAccent") or doc.get("accentColor") or "#ff6b2c"
    industry = doc.get("industry") or "this industry"
    marker = {
        "_placeholder": True,
        "_note": "Prep-only skeleton from seed_client_context.py --skeleton. Not real client data.",
    }

    return {
        "landing/brand": (
            f"clients/{slug}/landing/brand.json",
            {
                **marker,
                "client": slug,
                "company": name,
                "tokens": {
                    "colors": {"ink": "#141414", "paper": "#FAFAFA", "accent": accent},
                    "ground": "light",
                    "ratio": "3:2",
                    "roles": {"ground": "paper", "fg": "ink", "accent": "accent"},
                },
                "fonts": {
                    "display": guidelines.get("fontHeading") or "Inter",
                    "body": guidelines.get("fontBody") or "Inter",
                },
                "brandLaw": ["Placeholder brand law — prep only."],
                "carryForward": [],
                "references": [],
            },
        ),
        "landing/intake": (
            f"clients/{slug}/landing/intake.json",
            {
                **marker,
                # Built by joining lines rather than embedding escapes, so the
                # markdown stays readable here and cannot be mangled by one.
                "markdown": LINE_BREAK.join(
                    [
                        f"# {name} - landing page intake (prep skeleton)",
                        "",
                        "## Who we are",
                        "",
                        f"{name} operates in {industry}.",
                        "",
                        "## What this page is for",
                        "",
                        "A prep-only placeholder so landing-builder-agent has an intake to",
                        "read. Nothing here was written by the client.",
                        "",
                        "## Sections we want",
                        "",
                        "nav, hero, footer.",
                        "",
                    ]
                ),
            },
        ),
        "memory/beliefs": (
            f"clients/{slug}/memory/beliefs.json",
            {
                **marker,
                # branded-shorts-agent gates on this key existing at all.
                "brandedShortsLockedStyle": "prep-skeleton",
            },
        ),
        # A LIST, not a dict — the only extra with that shape, which is why this
        # function's return type is `tuple[str, Any]`. See
        # `skeleton_topics_catalog` for why the placeholder marker rides on each
        # row instead of on an envelope.
        "topics/catalog": (
            f"clients/{slug}/topics/catalog.json",
            skeleton_topics_catalog(doc, slug),
        ),
    }


def _project_context_docs(
    *,
    db: Any,
    bucket: Any,
    client_id: str,
    slug: str,
    projected_at: str,
    projected_by: str,
    report: Report,
    dry_run: bool,
) -> None:
    """Project one client's context documents into ``context/<docType>.json``.

    The query names the tier. It is not a filter applied afterwards, because a
    post-filter is one refactor away from becoming a fallback.
    """

    query = (
        db.collection("clientContextDocs")
        .where("clientId", "==", client_id)
        .where("tier", "==", PROJECTED_TIER)
    )
    by_type = {}
    for snapshot in query.stream():
        row = snapshot.to_dict() or {}
        doc_type = row.get("docType")
        if doc_type in PROJECTED_DOC_TYPES:
            by_type[doc_type] = (snapshot.id, row)

    for doc_type in PROJECTED_DOC_TYPES:
        found = by_type.get(doc_type)
        if found is None:
            # Not reported as a gap: most clients legitimately have only some
            # of the nine, and nine "absent" lines per client would bury the
            # ones that matter. The readiness report is where absence is
            # measured, per client per document.
            continue
        firestore_doc_id, row = found
        record = build_context_record(
            row,
            doc_type=doc_type,
            firestore_doc_id=firestore_doc_id,
            projected_at=projected_at,
            projected_by=projected_by,
        )
        if record is None:
            report.record("skipped", f"context/{doc_type} (empty content)")
            continue

        path = context_path(slug, doc_type)
        if dry_run:
            report.record("created", f"context/{doc_type} -> {path}")
            continue

        blob = bucket.blob(path)
        existing = blob.download_as_text() if blob.exists() else None
        if context_record_is_current(existing, record):
            # A no-op by contentHash, which means projectedAt is left ALONE.
            # Rewriting an identical document with a fresh timestamp would make
            # every run look like a change to anything reading that field.
            report.record("unchanged", f"context/{doc_type}")
            continue

        blob.upload_from_string(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
            content_type="application/json",
        )
        report.record(
            "updated" if existing else "created",
            f"context/{doc_type} (v{record['source']['docVersion']}, "
            f"{len(record['markdown'])} chars)",
        )


def _project_competitors(
    *,
    db: Any,
    bucket: Any,
    client_id: str,
    slug: str,
    report: Report,
    dry_run: bool,
) -> None:
    """Project ``clientCompetitors`` into the path ``client.listCompetitors`` reads."""

    rows = [
        snapshot.to_dict() or {}
        for snapshot in db.collection("clientCompetitors")
        .where("clientId", "==", client_id)
        .stream()
    ]
    competitors = build_competitors(rows)
    if not competitors:
        # An empty list is NOT written. `client.listCompetitors` treats a
        # present-but-empty array as a normal success with no competitors, and
        # a missing file as "never onboarded" -- so writing [] would convert an
        # honest "we have not set this up" into "we looked, there are none".
        report.record("skipped", "client/competitors (no rows in the portal)")
        return

    path = competitors_path(slug)
    body = json.dumps(competitors, ensure_ascii=False, indent=2, sort_keys=True)
    if dry_run:
        report.record("created", f"client/competitors ({len(competitors)}) -> {path}")
        return

    blob = bucket.blob(path)
    if blob.exists():
        if _sha(blob.download_as_text()) == _sha(body):
            report.record("unchanged", "client/competitors")
            return
        outcome = "updated"
    else:
        outcome = "created"
    blob.upload_from_string(body, content_type="application/json")
    report.record(outcome, f"client/competitors ({len(competitors)})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), required=True)
    parser.add_argument(
        "--carry-config-from",
        choices=sorted(ENVIRONMENTS),
        help=(
            "Copy each client's existing client/config.json from this "
            "environment when the target has none"
        ),
    )
    parser.add_argument(
        "--skeleton",
        action="store_true",
        help=(
            "Write a placeholder client/config.json for clients that have none, so every "
            "agent can run. PREP ONLY -- refused against prod."
        ),
    )
    parser.add_argument(
        "--projected-by",
        default="seed-cli",
        choices=("seed-cli", "backfill", "portal-save"),
        help=(
            "Recorded in each context record's provenance. The reader never "
            "branches on it; it is there so the audit trail can answer which "
            "mechanism wrote a projection."
        ),
    )
    parser.add_argument("--only", help="Restrict to one client slug")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.skeleton and args.env == "prod":
        sys.exit(
            "--skeleton writes invented configuration and is refused against production. "
            "A production client's handles, subreddits and content pillars are decisions "
            "a human makes; a placeholder there is indistinguishable from one of those."
        )

    target = ENVIRONMENTS[args.env]
    try:
        # Namespace packages; same note as seed_legacy_agents.py.
        from google.cloud import firestore, storage  # type: ignore[attr-defined]
    except ImportError:
        sys.exit("google-cloud-firestore and google-cloud-storage must be installed")

    db = firestore.Client(project=FIRESTORE_PROJECT, database=target["database"])
    storage_client = storage.Client()
    bucket = storage_client.bucket(target["bucket"])
    source_bucket = (
        storage_client.bucket(ENVIRONMENTS[args.carry_config_from]["bucket"])
        if args.carry_config_from
        else None
    )

    print(f"Projecting client context into gs://{target['bucket']}/clients/<slug>/client/")
    print(f"  source   : firestore {FIRESTORE_PROJECT}/{target['database']} (clients)")
    if args.carry_config_from:
        print(f"  config   : carried from {args.carry_config_from} when absent here")
    print(f"  mode     : {'DRY RUN' if args.dry_run else 'WRITING'}\n")

    report = Report()
    # One timestamp for the whole pass: two documents projected in the same run
    # were projected at the same moment, and a per-write `utcnow()` would make
    # them differ by milliseconds for no reason anyone could use.
    projected_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for snapshot in db.collection("clients").stream():
        doc = snapshot.to_dict() or {}
        slug = doc.get("agentsRepoSlug")
        if not slug:
            report.record("skipped", f"{doc.get('name') or snapshot.id} (no agentsRepoSlug)")
            continue
        if slug in TEST_SLUGS:
            report.record("skipped", f"{slug} (test fixture)")
            continue
        if args.only and slug != args.only:
            continue

        print(f"{slug}  ({doc.get('name')})")

        _project_context_docs(
            db=db,
            bucket=bucket,
            client_id=snapshot.id,
            slug=slug,
            projected_at=projected_at,
            projected_by=args.projected_by,
            report=report,
            dry_run=args.dry_run,
        )
        _project_competitors(
            db=db,
            bucket=bucket,
            client_id=snapshot.id,
            slug=slug,
            report=report,
            dry_run=args.dry_run,
        )

        records: dict[str, dict[str, Any]] = {
            "profile": build_profile(doc),
            "brand": build_brand(doc),
            "voice-rules": build_voice_rules(doc),
        }

        # config.json is engine-specific and not derivable from a portal
        # field, so it is only ever carried forward, never generated.
        if source_bucket is not None:
            src = source_bucket.blob(f"clients/{slug}/client/config.json")
            if src.exists():
                records["config"] = json.loads(src.download_as_text())

        # Only for clients that still have no config after the carry-forward:
        # a real one must never be overwritten by a placeholder.
        if args.skeleton and "config" not in records:
            if bucket.blob(f"clients/{slug}/client/config.json").exists():
                report.record("skipped", "config (already present, left alone)")
            else:
                records["config"] = skeleton_config(doc, slug)

        # Records that live outside clients/<slug>/client/, written directly
        # because the loop below only knows that one prefix.
        if args.skeleton:
            for label, (path, payload) in skeleton_extras(doc, slug).items():
                blob = bucket.blob(path)
                if blob.exists():
                    # Never overwritten, only ever created. A topics catalog in
                    # particular carries `reserved`/`committed` rows once runs
                    # have used it — re-seeding it would resurrect topics the
                    # client has already been posted about, which is the exact
                    # thing the catalog exists to prevent.
                    report.record("skipped", f"{label} (already present)")
                    continue
                if args.dry_run:
                    report.record("created", f"{label} -> {path}")
                    continue
                blob.upload_from_string(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    content_type="application/json",
                )
                report.record("created", label)

        for key, payload in records.items():
            if not payload:
                report.record("skipped", f"{key} (no data in the client record)")
                continue
            body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            path = f"clients/{slug}/client/{key}.json"
            if args.dry_run:
                report.record("created", f"{key} ({len(payload)} fields) -> {path}")
                continue
            blob = bucket.blob(path)
            if blob.exists():
                if _sha(blob.download_as_text()) == _sha(body):
                    report.record("unchanged", key)
                    continue
                outcome = "updated"
            else:
                outcome = "created"
            blob.upload_from_string(body, content_type="application/json")
            report.record(outcome, f"{key} ({len(payload)} fields)")

    print("\n" + "-" * 60)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(report.counts.items()))
    print("summary: " + (summary or "nothing to do"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
