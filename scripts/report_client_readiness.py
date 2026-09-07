#!/usr/bin/env python3
"""Report which agent-engine workflows each client can actually run.

## Why this exists instead of a production ``--skeleton``

``seed_client_context.py --skeleton`` writes placeholders for everything an
agent refuses to start without, and it is refused against production on
purpose. The placeholder file says so itself::

    "Prep-only skeleton written by seed_client_context.py --skeleton so every
     agent can run without blocked_intake. Not real client configuration;
     never seed this into production."

The reason is worth restating, because "just seed the configs" is a natural
thing to ask for. The blocking values are *channel identities*: which X
account posts, which subreddits get replied to, which Instagram styling has
been approved, which shows a client holds the rights to clip. None of those is
derivable from anything in the portal -- the client record has no such fields
-- so seeding them means inventing them.

Inventing them does not fail safe. A ``blocked_intake`` is loud and harmless.
An agent configured with a guessed ``xHandle`` **succeeds**: it produces
finished drafts aimed at an account nobody verified, and hands them to a
reviewer with no signal that the target was made up. Prep's own skeleton
guesses ``targetSubreddits: ["r/test"]`` and derives ``xHandle`` from the
client slug, which is exactly the shape of that failure.

So this script reports the gap rather than papering over it. It answers "which
clients are ready, and for the ones that are not, what is a human actually
required to supply" -- and it names the mechanism that supplies each thing,
because for most of them one already exists.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

ENVIRONMENTS: dict[str, str] = {
    "prep": "karoscmo-prep-agent-artifacts",
    "prod": "karoscmo-prod-agent-artifacts",
}


@dataclass(frozen=True)
class Requirement:
    """One workspace record an agent needs, and where it is supposed to come from."""

    #: Path under ``clients/<slug>/``, without the ``.json``.
    path: str
    #: Config keys that must be present when ``path`` is ``client/config``.
    keys: tuple[str, ...] = ()
    #: How a human is meant to supply this. Printed next to every gap.
    supplied_by: str = ""
    #: True when the agent degrades rather than stops. Reported as a note, not
    #: a block -- calling a soft gap "blocked" makes the report useless by
    #: crying wolf on agents that would actually produce something.
    soft: bool = False


#: Records projected from the portal by ``seed_client_context.py`` (no
#: ``--skeleton``). These are real data and every production client has them.
PROFILE = Requirement(
    "client/profile", supplied_by="seed_client_context.py (from the portal record)"
)
BRAND = Requirement("client/brand", supplied_by="seed_client_context.py (from the portal record)")
VOICE = Requirement(
    "client/voice-rules", supplied_by="seed_client_context.py (from the portal record)"
)

#: The forward pipeline every channel agent reserves a subject from. Nothing in
#: either repository writes it (``topics.topUp``'s one production caller passes
#: an empty list), so a client without it holds at the lane floor on every
#: channel agent -- including clients that are otherwise fully configured.
#: Hard for the two agents that stop without it; soft for the five that fall
#: through to a research-derived candidate instead (each says so in its own
#: comment at the reserve call). The distinction is the difference between
#: "produces nothing" and "produces without the no-repeat guarantee".
TOPICS = Requirement(
    "topics/catalog", supplied_by="NOTHING WRITES THIS YET -- see seed_client_context.py's own note"
)
TOPICS_SOFT = Requirement(
    "topics/catalog",
    supplied_by=(
        "NOTHING WRITES THIS YET -- this agent falls through to a research-derived "
        "candidate, so it runs WITHOUT the no-repeat guarantee"
    ),
    soft=True,
)


def cfg(*keys: str, supplied_by: str) -> Requirement:
    return Requirement("client/config", keys=keys, supplied_by=supplied_by)


SETUP_AGENT = "run the matching setup agent (linkedin-setup-agent / reddit-setup-agent)"
HUMAN_DECISION = "a person: this is an identity or a rights decision, not a derivable field"
ONBOARDING = "the one-time per-client onboarding workflow for this product"

#: Every routed engine product, and what it needs in the workspace before it
#: can produce anything.
#:
#: ``linkedin-setup-agent`` and ``reddit-setup-agent`` were removed here when
#: their workflow was inlined into each parent as its ``00-channel-setup``
#: pre-flight and they were retired from the catalog. Their requirements did
#: not vanish -- they moved into the parents, which is why
#: ``strategy/linkedin-agent`` is still listed under ``linkedin-agent``.
#:
#: They were worse than stale. Both were declared with NO requirements, so
#: ``gaps_for`` returned nothing for them and every client scored two free
#: `ready` products -- two agents that could not fail because they could not
#: run, counted in the numerator AND the denominator of "N/M client-agent pairs
#: can run today". The headline number was reading better than reality by two
#: per client. Derived from each agent's own intake steps, not
#: guessed -- the `client.*` calls and `WorkflowBlockedIntake` messages in
#: `agents/<name>/src/workflow/`.
AGENT_REQUIREMENTS: dict[str, tuple[Requirement, ...]] = {
    "x-agent": (
        PROFILE,
        BRAND,
        VOICE,
        cfg("xHandle", supplied_by=HUMAN_DECISION),
        Requirement("strategy/x-agent", supplied_by=SETUP_AGENT),
        TOPICS_SOFT,
    ),
    "linkedin-agent": (
        PROFILE,
        BRAND,
        VOICE,
        Requirement("strategy/linkedin-agent", supplied_by="linkedin-setup-agent"),
        TOPICS_SOFT,
    ),
    "reddit-agent": (
        PROFILE,
        BRAND,
        VOICE,
        cfg("targetSubreddits", supplied_by="reddit-setup-agent"),
        TOPICS_SOFT,
    ),
    "blog-agent": (
        PROFILE,
        BRAND,
        VOICE,
        cfg(
            "contentPillars",
            "targetKeywords",
            supplied_by="a person: the editorial plan, not a restatement of the industry field",
        ),
        TOPICS_SOFT,
    ),
    "newsletter-agent": (
        PROFILE,
        BRAND,
        VOICE,
        cfg("targetAudience", "frequency", supplied_by="a person: the editorial plan"),
        TOPICS_SOFT,
    ),
    "instagram-agent": (
        PROFILE,
        cfg(
            "instagramStyleConfig",
            "instagramBrandTokens",
            supplied_by=ONBOARDING
            + " -- instagram-agent refuses to guess styling and blocks instead",
        ),
        TOPICS,
    ),
    "reputation-agent": (PROFILE, BRAND, VOICE),
    "seo-geo-agent": (PROFILE, BRAND),
    "intel-report-agent": (PROFILE, BRAND),
    "landing-builder-agent": (
        Requirement("landing/brand", supplied_by=ONBOARDING),
        Requirement("landing/intake", supplied_by=ONBOARDING),
    ),
    "branded-shorts-agent": (
        BRAND,
        cfg(
            "brandedShortsProfilePath",
            "brandedShortsGraphicsLanguage",
            "brandedShortsApprovedArchetypes",
            supplied_by="the Style Exploration onboarding workflow",
        ),
    ),
    "tiktok-agent": (
        BRAND,
        VOICE,
        cfg(
            "tiktokClips",
            supplied_by=HUMAN_DECISION + " (which shows this client holds the rights to clip)",
        ),
        TOPICS,
    ),
}


@dataclass
class ClientState:
    slug: str
    objects: set[str] = field(default_factory=set)
    config: dict[str, object] = field(default_factory=dict)
    placeholder: bool = False


def load_client(storage: Any, bucket_name: str, slug: str) -> ClientState:
    import json

    bucket = storage.bucket(bucket_name)
    state = ClientState(slug)
    prefix = f"clients/{slug}/"
    for blob in storage.list_blobs(bucket, prefix=prefix):
        state.objects.add(blob.name[len(prefix) :].removesuffix(".json"))
    blob = bucket.blob(f"{prefix}client/config.json")
    if blob.exists():
        try:
            state.config = json.loads(blob.download_as_text())
        except ValueError:
            state.config = {}
        state.placeholder = bool(state.config.get("_placeholder"))
    return state


def readiness_paths(agent: str) -> tuple[list[str], list[str]]:
    """This agent's requirements as flat path strings, hard and soft.

    Exported so ``seed_all_agents.py`` can put ``readiness`` on the C4
    descriptor without retyping the table. One source, two consumers: a second
    copy would drift, and the copy that drifted would be the one a planner uses
    to tell a client whether they can have a post today.

    A config key renders as ``client/config:xHandle`` -- close to the shape the
    report prints, so a reader comparing the two sees the same strings.
    """

    hard: list[str] = []
    soft: list[str] = []
    for req in AGENT_REQUIREMENTS[agent]:
        names = (
            [f"client/config:{key}" for key in req.keys]
            if req.path == "client/config"
            else [req.path]
        )
        (soft if req.soft else hard).extend(names)
    unique_hard = list(dict.fromkeys(hard))
    # A path that is hard for one requirement and soft for another is hard:
    # topics/catalog is soft for five agents and hard for two, and an agent
    # holding both should not advertise it as merely degrading.
    return unique_hard, [p for p in dict.fromkeys(soft) if p not in unique_hard]


def gaps_for(state: ClientState, agent: str) -> tuple[list[str], list[str]]:
    """Unmet requirements for one agent, split into blocking and degrading."""
    out: list[str] = []
    soft: list[str] = []
    for req in AGENT_REQUIREMENTS[agent]:
        if req.path == "client/config":
            missing = [k for k in req.keys if not state.config.get(k)]
            if missing:
                (soft if req.soft else out).append(
                    f"client/config: {', '.join(missing)}  <- {req.supplied_by}"
                )
            continue
        # A strategy requirement is met by ANY document under that prefix: an
        # agent-level charter and a per-seat one both count.
        if req.path.startswith("strategy/"):
            if not any(o == req.path or o.startswith(req.path + "/") for o in state.objects):
                (soft if req.soft else out).append(f"{req.path}: absent  <- {req.supplied_by}")
            continue
        if req.path not in state.objects:
            (soft if req.soft else out).append(f"{req.path}: absent  <- {req.supplied_by}")
    return out, soft


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), required=True)
    parser.add_argument("--client", action="append", help="Restrict to one slug; repeatable.")
    parser.add_argument("--agent", action="append", help="Restrict to one product id; repeatable.")
    args = parser.parse_args()

    try:
        from google.cloud import firestore, storage  # type: ignore[attr-defined]
    except ImportError:
        sys.exit("google-cloud-firestore and google-cloud-storage must be installed")

    bucket_name = ENVIRONMENTS[args.env]
    db = firestore.Client(
        project="karoscmo", database="prep" if args.env == "prep" else "(default)"
    )
    discovered: list[str] = []
    for snapshot in db.collection("clients").stream():
        record = snapshot.to_dict()
        if record and record.get("agentsRepoSlug"):
            discovered.append(str(record["agentsRepoSlug"]))
    slugs = args.client or sorted(discovered)
    agents = args.agent or sorted(AGENT_REQUIREMENTS)

    client = storage.Client()
    print(f"Workspace readiness — {args.env} (gs://{bucket_name})\n")

    ready_count = 0
    total = 0
    for slug in slugs:
        state = load_client(client, bucket_name, slug)
        note = "  [config is a PREP PLACEHOLDER, not real]" if state.placeholder else ""
        blocked: dict[str, list[str]] = {}
        degraded: dict[str, list[str]] = {}
        for agent in agents:
            gap, soft = gaps_for(state, agent)
            total += 1
            if gap:
                blocked[agent] = gap
            else:
                ready_count += 1
                if soft:
                    degraded[agent] = soft
        ready = [a for a in agents if a not in blocked]
        print(f"{slug}{note}")
        print(f"  can run ({len(ready)}/{len(agents)}): {', '.join(ready) or 'none'}")
        for agent, gap in degraded.items():
            print(f"  degraded {agent}")
            for line in gap:
                print(f"      {line}")
        for agent, gap in blocked.items():
            print(f"  BLOCKED  {agent}")
            for line in gap:
                print(f"      {line}")
        print()

    print("-" * 72)
    print(f"{ready_count}/{total} client-agent pairs can run today")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
