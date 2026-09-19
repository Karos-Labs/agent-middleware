#!/usr/bin/env python3
"""What every model-running step costs per million tokens (L2 M4 / SCRUM-346).

M4 asks for "the top 10 most expensive steps". **That table cannot be built
from this repository, and this script does not pretend to build it.** The
ranking M4 wants is by DOLLARS ACTUALLY SPENT, which is rate times volume, and
the volume half — how many tokens each step really consumes — exists only in
telemetry written at execution time (`bi_telemetry.agent_runs_bi`). Nothing in
any of the three repos records it: the one cost fixture that exists is a single
synthetic run in the engine's `cost-accuracy-golden.test.ts`, and the golden
runs under `evals/` carry no cost fields at all.

So this produces the half that IS derivable, and says so in its own output:

    every step that runs a model, its model, and that model's rate

which is the M4 table with the RATE column real and the VOLUME column pending.
It is worth having on its own, for three reasons that do not need volume:

* It finds steps running a model more expensive than the work needs. A
  classification step on Opus costs five times the same step on Haiku
  regardless of how many tokens it sees, and that is M6's whole list.
* It finds the same job priced differently across agents — two agents doing
  the equivalent step on different models is a decision nobody made.
* It is the denominator for M3. When the baseline runs produce token counts,
  joining them to this table is a multiplication, not another investigation.

## Where the two halves come from

`engine_stages.json` — generated from agent-engine's own workflow sources by
`generate_engine_stages.py`, so the step list is derived rather than authored,
and `--check` fails when the engine moves ahead of it.

`seed_models.py`'s `CATALOG` — the verified price list, each row carrying the
source it was read from and the day it was read. Deliberately NOT the engine's
`MODEL_PRICING`: that is a hard-coded table in another repo, and S12 moved the
prices here precisely because three of its rows were wrong. Where a step's
`default_model` names a provider model rather than a catalog id, the two are
joined on `provider_model_name`.

## The mechanical/creative split, and why it is a guess this script labels

M6 wants "mechanical steps" downgraded. Nothing in the catalogue says which
steps are mechanical, so this classifies by what the step's ID says it does:
a step called `verify-*` or `classify-*` is doing a job whose output is a
verdict, and a step called `draft-*` or `write-*` is producing the thing the
client reads. That is a heuristic, it is labelled `guess` in the output, and it
exists to give M6 a starting list to argue with — not to authorise a swap. A
step's model should change because someone looked at what it does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STAGES = HERE / "engine_stages.json"

#: Step ids whose work is a verdict, a label or a lookup -- the output is
#: consumed by code, not read by a person. These are M6's candidates.
MECHANICAL_PATTERNS: tuple[str, ...] = (
    r"verify",
    r"check",
    r"classif",
    r"extract",
    r"validate",
    r"detect",
    r"score",
    r"select",
    r"rank",
    r"dedupe",
    r"summari[sz]e",
    r"parse",
    r"triage",
    r"route",
)

#: ...and the ones whose output IS the deliverable, or shapes it. A cheaper
#: model here is a cheaper product.
CREATIVE_PATTERNS: tuple[str, ...] = (
    r"draft",
    r"write",
    r"compose",
    r"craft",
    r"caption",
    r"script",
    r"headline",
    r"copy",
    r"design",
    r"narrat",
    r"story",
    r"idea",
    r"strategy",
    r"revise",
    r"edit",
)


def classify(step_id: str) -> str:
    """``mechanical`` | ``creative`` | ``unclassified``, from the step's name.

    Mechanical is checked first on purpose: ``verify-headline`` is a check,
    not a headline. A step matching neither is left ``unclassified`` rather
    than defaulted, because a wrong default here is a swap nobody argued for.
    """

    name = step_id.lower()
    if any(re.search(p, name) for p in MECHANICAL_PATTERNS):
        return "mechanical"
    if any(re.search(p, name) for p in CREATIVE_PATTERNS):
        return "creative"
    return "unclassified"


def load_catalog() -> dict[str, dict[str, Any]]:
    """The priced rows of ``seed_models.py``, keyed by every id that can reach them.

    Imported rather than re-parsed: the prices have exactly one home (S12), and
    a second copy here would be the drift that ticket existed to end.

    Keyed twice on purpose. A stage's ``default_model`` is what the engine
    compiled into the agent -- a PROVIDER model name like
    ``claude-sonnet-4-6`` -- while the catalog is keyed on our own
    ``model_id`` (``claude-sonnet-4-6-on-vertex``). Joining on only one of them
    silently drops most of the fleet.
    """

    sys.path.insert(0, str(HERE))
    from seed_models import CATALOG, UNPRICED  # noqa: PLC0415

    by_id: dict[str, dict[str, Any]] = {}
    for entry in (*CATALOG, *UNPRICED):
        row = dict(entry)
        # THREE STATES, NOT TWO, and the third is the one worth finding.
        # `CATALOG` is priced. `UNPRICED` is a model somebody looked at and
        # decided could not be priced from a primary source -- a deliberate,
        # recorded choice. A model in NEITHER list is a step running on
        # something the config plane has never heard of, which is not a
        # decision, it is a gap. Collapsing the last two would hide it.
        row["_known"] = True
        row["_priced"] = row.get("input_per_1m") is not None
        by_id[str(row["model_id"])] = row
        provider = row.get("provider_model_name")
        # `setdefault`: when two rows share a provider name (the same model on
        # two routes) the first wins, and they carry the same price by
        # construction -- `seed_models`' own checks refuse a price that differs.
        if isinstance(provider, str) and provider:
            by_id.setdefault(provider, row)
    return by_id


def model_steps(stages: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``kind: "agent"`` step across every agent, flattened.

    ``code`` and ``gate`` steps are dropped: they cost nothing per token. A
    gate can still cost a person's time, which is M8's subject, not M4's.
    """

    out: list[dict[str, Any]] = []
    for agent, steps in sorted(stages.items()):
        for step in steps:
            if not isinstance(step, dict) or step.get("kind") != "agent":
                continue
            out.append(
                {
                    "agent": agent,
                    "step": step.get("id"),
                    "agentId": step.get("agent_id"),
                    "skillRef": step.get("skill_ref"),
                    "model": step.get("default_model"),
                    "vendor": step.get("vendor"),
                    "work": classify(str(step.get("id") or "")),
                }
            )
    return out


def priced(rows: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Join each step to its model's rate, leaving an unpriced step unpriced.

    An unpriced row is reported rather than dropped or defaulted. A step whose
    model is not in the catalog is either a model nobody verified a price for
    or a stale `engine_stages.json`, and both are findings -- defaulting it to
    Sonnet's rate would bury them under a plausible number.
    """

    for row in rows:
        entry = catalog.get(str(row["model"]))
        if entry is None:
            row["priceState"] = "absent"
            row["inputPer1M"] = None
            row["outputPer1M"] = None
            row["blendedPer1M"] = None
            row["pricedFrom"] = None
            continue
        row["priceState"] = "priced" if entry["_priced"] else "unpriced-on-purpose"
        if not entry["_priced"]:
            row["inputPer1M"] = None
            row["outputPer1M"] = None
            row["blendedPer1M"] = None
            row["pricedFrom"] = entry.get("model_id")
            continue
        row["inputPer1M"] = entry.get("input_per_1m")
        row["outputPer1M"] = entry.get("output_per_1m")
        row["pricedFrom"] = entry.get("model_id")
        # A single number to sort by, and the weighting is stated rather than
        # hidden: a drafting step reads far more than it writes, so input is
        # the larger share. It is a SORT KEY for a table whose real ordering
        # needs volume -- not a cost estimate, and not presented as one.
        if row["inputPer1M"] is not None and row["outputPer1M"] is not None:
            row["blendedPer1M"] = round(0.75 * row["inputPer1M"] + 0.25 * row["outputPer1M"], 4)
        else:
            row["blendedPer1M"] = None
    return rows


BLEND_NOTE = (
    "sorted by 0.75*input + 0.25*output per 1M tokens -- a RATE, not a spend: "
    "the token volumes M4 needs to rank by dollars live only in run telemetry"
)


def render(rows: list[dict[str, Any]], *, limit: int | None) -> str:
    lines: list[str] = []
    lines.append("# What every model-running step costs (L2 M4 / SCRUM-346)")
    lines.append("")
    lines.append(
        f"{len(rows)} model-running steps across "
        f"{len({r['agent'] for r in rows})} agents. "
        f"Rates from `seed_models.py`; steps from `engine_stages.json`."
    )
    lines.append("")
    lines.append(f"**This is a rate table, not a spend table.** {BLEND_NOTE}.")
    lines.append("")

    ranked = sorted(
        rows,
        key=lambda r: (r["blendedPer1M"] is None, -(r["blendedPer1M"] or 0), r["agent"], r["step"]),
    )
    shown = ranked[:limit] if limit else ranked

    lines.append("| # | agent | step | work | model | $/1M in | $/1M out |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, row in enumerate(shown, 1):
        rate_in = "?" if row["inputPer1M"] is None else f"{row['inputPer1M']:.2f}"
        rate_out = "?" if row["outputPer1M"] is None else f"{row['outputPer1M']:.2f}"
        lines.append(
            f"| {i} | {row['agent']} | `{row['step']}` | {row['work']} | "
            f"`{row['model']}` | {rate_in} | {rate_out} |"
        )
    lines.append("")

    absent = [r for r in rows if r["priceState"] == "absent"]
    if absent:
        lines.append(f"## {len(absent)} step(s) run a model the config plane has never heard of")
        lines.append("")
        lines.append(
            "Not in `CATALOG` and not in `UNPRICED` — so this is not a decision anybody "
            "recorded, it is a gap. These steps cannot be priced, budgeted or compared, "
            "and nothing fails to tell you."
        )
        lines.append("")
        for row in absent:
            lines.append(f"* {row['agent']} · `{row['step']}` → `{row['model']}`")
        lines.append("")

    on_purpose = [r for r in rows if r["priceState"] == "unpriced-on-purpose"]
    if on_purpose:
        models = sorted({str(r["model"]) for r in on_purpose})
        lines.append(f"## {len(on_purpose)} step(s) on a model deliberately left unpriced")
        lines.append("")
        lines.append(
            f"In `UNPRICED` ({', '.join(f'`{m}`' for m in models)}) — somebody looked and "
            "recorded that no primary source publishes a rate. That is a defensible "
            "choice per model; what this table adds is how much of the fleet now "
            "depends on it, which is the number that decides whether it stays defensible."
        )
        lines.append("")
        for row in on_purpose:
            lines.append(f"* {row['agent']} · `{row['step']}` → `{row['model']}`")
        lines.append("")

    mechanical = [r for r in rows if r["work"] == "mechanical" and r["blendedPer1M"] is not None]
    if mechanical:
        dearest = sorted(mechanical, key=lambda r: -(r["blendedPer1M"] or 0))
        lines.append("## M6's starting list: mechanical steps, dearest first")
        lines.append("")
        lines.append(
            "Classified from the step's own id (`verify-*`, `classify-*`, ...), which is a "
            "**guess**. It is a list to argue with, not a list to act on: a step's model "
            "should change because somebody looked at what it does."
        )
        lines.append("")
        lines.append("| agent | step | model | $/1M in |")
        lines.append("|---|---|---|---|")
        for row in dearest[: limit or 20]:
            lines.append(
                f"| {row['agent']} | `{row['step']}` | `{row['model']}` | {row['inputPer1M']:.2f} |"
            )
        lines.append("")

    by_model: dict[str, int] = {}
    for row in rows:
        by_model[str(row["model"])] = by_model.get(str(row["model"]), 0) + 1
    lines.append("## Steps per model")
    lines.append("")
    lines.append("| model | steps | $/1M in |")
    lines.append("|---|---|---|")
    for model, count in sorted(by_model.items(), key=lambda kv: -kv[1]):
        rate = next((r["inputPer1M"] for r in rows if r["model"] == model), None)
        lines.append(f"| `{model}` | {count} | {'?' if rate is None else f'{rate:.2f}'} |")
    lines.append("")
    return "\n".join(lines)


def build(limit: int | None = None) -> tuple[list[dict[str, Any]], str]:
    stages = json.loads(STAGES.read_text())
    rows = priced(model_steps(stages), load_catalog())
    return rows, render(rows, limit=limit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit rows as JSON instead of markdown")
    parser.add_argument("--limit", type=int, default=None, help="show only the N dearest steps")
    args = parser.parse_args()

    rows, table = build(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
