"""Which post goes in each slot, and why (N4).

Cadence is the client's: their calendar says a post goes out on Tuesday. What
has never existed anywhere is the answer to the next question -- *which* post --
and in its absence every run picked its own subject from the strategy map in
whatever order the map happened to be in. Two "attention" posts in a row, three
promotional posts in a week, and a "timely" post about nothing in particular are
all things that could happen and had nothing to stop them.

This is the thing that stops them. It takes what the control plane already
holds -- the strategy map with a stage on every row (C1), the subject rows for
what has actually gone out (B1), the client's default mix (D32) -- and lays out
the next N slots.

WHY IT IS HERE AND NOT IN THE ENGINE. A run knows about itself. Sequencing is
the one decision that is only correct in the light of the *other* posts: the one
that went out yesterday and the one going out on Thursday. The middleware is the
only place where the map and the history are joinable, so it is the only place
that can answer this without a run guessing.

WHY IT IS ARITHMETIC. Same reason as ``voice_lessons``: a model asked to
"sequence the week" produces an order nobody can check. Every rule below is a
countable property of the plan, so a reader can hold the plan next to the rules
and see for themselves.

THE FIVE RULES, and what each one actually does:

* **Never the same stage twice in a row, never the same type twice in a row.**
  The comparison starts at the last row that really went out, not at the top of
  the plan -- otherwise slot one repeats yesterday, which is the case a client
  would notice first.
* **The mix is a share, not a rotation.** D32 says three attention, two
  expertise, one decide per six. A rotation would produce A-E-D-A-E-D forever;
  what this does is give each slot to the stage furthest BEHIND its share,
  counting the history and the plan so far together. The mix is a target the
  sequence drifts back towards, which is what a mix means.
* **Promotional at most one in six**, counted over any six consecutive posts
  spanning the history and the plan -- not one per calendar week, which a plan
  could satisfy while putting two on consecutive days either side of a Sunday.
* **A timely post only when a real anchor exists.** No anchor, no timely slot.
  An anchor is consumed by the slot that uses it, so one piece of news cannot
  justify three posts.
* **A client request wins.** Outright: over the mix, and over the variety rules
  too. A client who asks for a post and gets it a slot later because the stage
  repeated has been overruled by a heuristic, which is not what "wins" means.
  The reason line says so, so nobody has to guess why the pattern broke.

WHAT THIS CANNOT DO YET, said out loud rather than faked. "Weight towards what
performs" needs the what-works summary, and the ingestion that produces it does
not exist (02 §3.4; ``learning.context`` still returns ``"what-works": None``).
So ``performance`` is an optional argument: given one, candidates are ordered by
it; given none, they are ordered by the map's own position and every slot's
reason says ``no performance data``. An ordering that silently fell back to
something else would look exactly like a working weighting.

TWO PRODUCTS THIS DOES NOT SPEAK FOR. Instagram formats and visuals are chosen
per post by performance and relevance and never by a ratio or a rotation (D17),
so a slot names the stage and the subject and stops -- picking the format here
would BE the rotation D17 forbids. And TikTok editing is on demand (D19): it has
no slots to fill, so ``sequences_product`` returns False for it and the caller
skips the platform rather than planning posts nobody asked for.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

#: The funnel, in the order D32 names it. Duplicated from ``learning_store``
#: deliberately: this module is pure and importing the store for a tuple would
#: drag asyncpg into a unit test.
STAGES: tuple[str, ...] = ("attention", "expertise", "decide")

#: What each stage is for, in the words the client's card uses. A run may write
#: its own goal line for the post it actually drafted; this is the goal the SLOT
#: was created to serve, and the two being different is information.
STAGE_GOALS: dict[str, str] = {
    "attention": "earn attention",
    "expertise": "show expertise",
    "decide": "help them decide",
}

#: D32's default, and the fallback when a client's map carries no mix of its own.
DEFAULT_MIX: dict[str, int] = {"attention": 3, "expertise": 2, "decide": 1}

#: The type that is capped. Everything else is uncapped.
PROMOTIONAL = "promotional"

#: ...and how tightly. One in any six consecutive posts.
PROMOTIONAL_WINDOW = 6
PROMOTIONAL_MAX = 1

#: The type that may only run behind a real anchor.
TIMELY = "timely"

#: Products with slots to fill. Everything else is on demand.
ON_DEMAND_PRODUCTS: frozenset[str] = frozenset({"tiktok-editing-agent", "branded-shorts-agent"})


def sequences_product(product_id: str | None) -> bool:
    """Whether this product has a sequence at all.

    D19: TikTok editing is on demand -- a client hands us footage and asks for a
    cut. It shares the TikTok platform (and therefore the subject history) with
    the two products that DO have slots, so the check is on the product, not the
    platform, or planning TikTok would plan for a product with no calendar.
    """

    return bool(product_id) and product_id not in ON_DEMAND_PRODUCTS


def open_rows(map_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map rows a slot may still be given, in the map's own order.

    ``used`` rows have already produced a post and ``retired`` ones were dropped
    by a rebuild; both are history. A row with a stage outside the funnel is
    dropped rather than defaulted, because a defaulted stage would silently
    distort the very mix this module exists to hold.
    """

    return [
        row
        for row in map_rows
        if isinstance(row, dict)
        and row.get("status", "open") == "open"
        and row.get("stage") in STAGES
        and isinstance(row.get("idea"), str)
        and row["idea"].strip()
    ]


def history(recent: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    """The subject window as this module reads it: OLDEST first.

    ``subject_window`` serves newest-first, which is right for a table a person
    scans and wrong for every rule here, all of which are about what follows
    what. Reversing it once, here, is cheaper than getting the direction wrong
    in four places.

    Skipped rows are dropped: a draft the client refused is not something the
    audience saw, so it neither repeats a stage nor spends the promotional slot.
    """

    out: list[dict[str, str | None]] = []
    for row in reversed(recent):
        if not isinstance(row, dict) or row.get("status") == "skipped":
            continue
        stage = row.get("stage")
        out.append(
            {
                "stage": stage if stage in STAGES else None,
                "type": row.get("type") if isinstance(row.get("type"), str) else None,
            }
        )
    return out


def mix_target(default_mix: dict[str, Any] | None) -> dict[str, int]:
    """The client's mix, falling back to D32's, with anything unusable dropped."""

    mix = {
        stage: int(value)
        for stage, value in (default_mix or {}).items()
        if stage in STAGES and isinstance(value, (int, float)) and value > 0
    }
    return mix or dict(DEFAULT_MIX)


def stage_deficit(counts: Counter[str], mix: dict[str, int]) -> list[tuple[float, str]]:
    """Every stage in the mix, most-owed first.

    The deficit is *share owed minus share had*, over the run of posts counted so
    far. On an empty history every stage is owed its full share, so the first
    slot goes to the largest share in the mix -- attention, by default, which is
    also what a person would pick to open with.
    """

    total = sum(counts.values())
    weight = sum(mix.values()) or 1
    scored = [
        ((mix[stage] / weight) - (counts.get(stage, 0) / total if total else 0.0), stage)
        for stage in mix
    ]
    # Most-owed first; the mix's own order breaks a tie, so the result is stable
    # rather than dependent on dict iteration on an empty history.
    order = {stage: i for i, stage in enumerate(mix)}
    scored.sort(key=lambda pair: (-pair[0], order[pair[1]]))
    return scored


def promotional_allowed(recent_types: list[str | None]) -> bool:
    """Whether a promotional post may go in the NEXT position.

    Counts the trailing ``PROMOTIONAL_WINDOW - 1`` posts: adding one more makes a
    window of six, which may hold at most ``PROMOTIONAL_MAX``.
    """

    tail = recent_types[-(PROMOTIONAL_WINDOW - 1) :] if PROMOTIONAL_WINDOW > 1 else []
    return tail.count(PROMOTIONAL) < PROMOTIONAL_MAX


def _usable(
    row: dict[str, Any],
    *,
    last_stage: str | None,
    last_type: str | None,
    types_so_far: list[str | None],
    anchors_left: int,
) -> bool:
    row_type = row.get("type")
    if row.get("stage") == last_stage:
        return False
    if row_type is not None and row_type == last_type:
        return False
    if row_type == PROMOTIONAL and not promotional_allowed(types_so_far):
        return False
    if row_type == TIMELY and anchors_left <= 0:
        return False
    return True


def _rank(rows: list[dict[str, Any]], performance: dict[str, float] | None) -> list[dict[str, Any]]:
    """Candidates in the order a slot should consider them.

    With a performance map, best first. Without one, the map's own order, which
    is the engine's judgement at build time and the only signal that exists.
    """

    if not performance:
        return list(rows)
    return sorted(rows, key=lambda row: -performance.get(str(row.get("id")), 0.0))


def _slot(
    *,
    position: int,
    stage: str,
    row: dict[str, Any] | None,
    subject: str,
    reason: str,
    why_now: str | None = None,
    source: str,
) -> dict[str, Any]:
    slot: dict[str, Any] = {
        "slot": position,
        "stage": stage,
        "subject": subject,
        "goal": STAGE_GOALS.get(stage, stage),
        "source": source,
        "reason": reason,
    }
    if row is not None:
        if row.get("id"):
            slot["rowId"] = row["id"]
        if isinstance(row.get("type"), str):
            slot["type"] = row["type"]
        if isinstance(row.get("problem"), str):
            slot["problem"] = row["problem"]
    if why_now:
        slot["whyNow"] = why_now
    return slot


def plan(
    *,
    slots: int,
    map_rows: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    default_mix: dict[str, Any] | None = None,
    requests: list[dict[str, Any]] | None = None,
    anchors: list[dict[str, Any]] | None = None,
    performance: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Lay out the next ``slots`` posts.

    Returns the plan AND what it could not do: ``unfilled`` is how many slots the
    open map could not cover, which is the signal that the map needs rebuilding
    before the client starts repeating themselves. A caller that reads only
    ``slots`` sees a short plan and cannot tell a quiet week from an empty pool.
    """

    mix = mix_target(default_mix)
    past = history(recent)
    pool = open_rows(map_rows)
    ranked = _rank(pool, performance)
    queue = [
        r for r in (requests or []) if isinstance(r, dict) and str(r.get("subject", "")).strip()
    ]
    anchors_left = [a for a in (anchors or []) if isinstance(a, dict)]

    counts: Counter[str] = Counter(row["stage"] for row in past if row["stage"])
    types_so_far: list[str | None] = [row["type"] for row in past]
    last_stage = past[-1]["stage"] if past else None
    last_type = past[-1]["type"] if past else None

    how_ranked = "by performance" if performance else "no performance data, map order"
    planned: list[dict[str, Any]] = []
    notes: list[str] = []
    unfilled = 0

    for position in range(1, max(0, slots) + 1):
        if queue:
            # A client request wins outright -- over the mix and over the
            # variety rules. Deferring it to keep a pattern is the portal
            # deciding it knows better than the person paying for the post.
            request = queue.pop(0)
            asked = request.get("stage")
            stage: str = (
                asked
                if isinstance(asked, str) and asked in STAGES
                else (last_stage_free(mix, counts))
            )
            slot = _slot(
                position=position,
                stage=stage,
                row=request,
                subject=str(request["subject"]).strip(),
                reason="the client asked for this post, which wins over the mix",
                why_now=request.get("whyNow") or "the client asked for it",
                source="client-request",
            )
            planned.append(slot)
            counts[stage] += 1
            types_so_far.append(slot.get("type"))
            last_stage, last_type = stage, slot.get("type")
            continue

        chosen: dict[str, Any] | None = None
        chosen_stage: str | None = None
        for _, stage in stage_deficit(counts, mix):
            for row in ranked:
                if row.get("stage") != stage:
                    continue
                if not _usable(
                    row,
                    last_stage=last_stage,
                    last_type=last_type,
                    types_so_far=types_so_far,
                    anchors_left=len(anchors_left),
                ):
                    continue
                chosen, chosen_stage = row, stage
                break
            if chosen is not None:
                break

        if chosen is None or chosen_stage is None:
            unfilled = slots - position + 1
            notes.append(
                f"{unfilled} slot(s) unfilled: no open strategy row fits the rules from here. "
                "The map needs rebuilding before this client repeats themselves."
            )
            break

        why_now = None
        if chosen.get("type") == TIMELY and anchors_left:
            anchor = anchors_left.pop(0)
            why_now = str(anchor.get("whyNow") or anchor.get("headline") or "").strip() or None

        planned.append(
            _slot(
                position=position,
                stage=chosen_stage,
                row=chosen,
                subject=str(chosen["idea"]).strip(),
                reason=f"{chosen_stage} was furthest behind the mix; ranked {how_ranked}",
                why_now=why_now,
                source="strategy-map",
            )
        )
        ranked = [row for row in ranked if row is not chosen]
        counts[chosen_stage] += 1
        types_so_far.append(chosen.get("type"))
        last_stage, last_type = chosen_stage, chosen.get("type")

    if not performance:
        notes.append(
            "Ranked by the map's own order: the what-works summary does not exist yet (02 §3.4)."
        )

    return {"slots": planned, "unfilled": unfilled, "mix": mix, "notes": notes}


def last_stage_free(mix: dict[str, int], counts: Counter[str]) -> str:
    """The stage a request with no stage of its own is filed under.

    A client request is a subject, not a funnel position, and refusing one for
    want of a stage would be absurd. It is filed where the mix is most owed, so
    an unlabelled request costs the mix nothing it was not going to spend.
    """

    return stage_deficit(counts, mix)[0][1]


__all__ = [
    "DEFAULT_MIX",
    "PROMOTIONAL",
    "PROMOTIONAL_MAX",
    "PROMOTIONAL_WINDOW",
    "STAGES",
    "STAGE_GOALS",
    "TIMELY",
    "history",
    "mix_target",
    "open_rows",
    "plan",
    "promotional_allowed",
    "sequences_product",
    "stage_deficit",
]
