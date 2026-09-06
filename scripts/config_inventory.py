#!/usr/bin/env python3
"""Configuration inventory for agent-middleware (AU52 / SCRUM-335).

Emits four views of this service's configuration and the deltas between them:

    READ BY CODE     every environment variable the service actually reads
    DOCUMENTED       what .env.example tells a person exists
    WIRED AT DEPLOY  what cloudbuild.yaml sets on the Cloud Run revision
    PARITY           whether prep and prod are wired from the same shape

Run it::

    python scripts/config_inventory.py            # full report
    python scripts/config_inventory.py --check    # CI mode, exit 1 on a hard delta
    python scripts/config_inventory.py --json     # machine-readable

Modelled on ``agent-engine``'s ``scripts/config-inventory.ts`` (AU49) so the
two repositories answer the same questions in the same words. Three checks here
have no counterpart there, and each exists because of something this repository
actually does -- see HARDCODED, SUBSTITUTIONS and PARITY below.

## Why this is not a grep -- and why the answer is different from agent-engine's

In agent-engine, most variables are read as ``process.env.X`` and eleven are
reached indirectly; that script pins those eleven by name as known false
positives.

Here the indirection is **total**. Every variable is a field on
``app.config.Settings``, a ``pydantic-settings`` model that maps a field to an
environment variable by name. ``GCP_PROJECT_ID`` never appears next to the word
``environ`` anywhere in ``app/`` -- only ``gcp_project_id: str`` does. So a
naive grep reports EVERY variable as wired-but-never-read, and a pinned list of
false positives would be the entire configuration surface and would need
editing on every new setting.

The read set is therefore *derived from the model* rather than matched in text,
and ``--check`` proves the derivation is doing work by running the naive grep
alongside it and asserting the grep is the weaker of the two. That
self-test cannot go stale, because it is recomputed rather than remembered.

## What it will never do

Recommend a deletion. WIRED-BUT-UNREAD warns and never fails: with the whole
config surface behind a model, absence of evidence is not evidence of absence,
and ``extra="ignore"`` on ``Settings`` means an unread variable is silently
tolerated rather than rejected. A variable is removed by a person who checked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Not application configuration: names the platform provides, or that exist
#: only to point a client library at an emulator. Documenting ``PATH`` in
#: .env.example would be noise that trains people to skim.
NOT_APP_CONFIG = frozenset(
    {
        "HOME",
        "PATH",
        "PORT",
        "LANG",
        "TMPDIR",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
    }
)

#: Substitutions that are build plumbing rather than application config: they
#: name the image and where it goes, and never reach the running container as
#: an environment variable.
BUILD_ONLY_SUBSTITUTIONS = frozenset({"_REGION", "_REPO", "_SERVICE", "_SERVICE_ACCOUNT"})


def _read(path: Path) -> str:
    """Read a repo file, normalising line endings.

    CRLF is not hypothetical here: this repository is developed on Windows, and
    a newline-anchored parser that silently matches nothing would report "0
    variables wired" and pass. A config checker that finds nothing is worse
    than no config checker, so this is not a tidiness measure.
    """

    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return ""


# --- READ BY CODE -----------------------------------------------------------


def settings_variables() -> dict[str, str]:
    """Every variable ``Settings`` reads, mapped to how it is declared.

    ``pydantic-settings`` resolves a field named ``gcp_project_id`` from
    ``GCP_PROJECT_ID`` without the string ever appearing in the source, so this
    is the authoritative read set and it cannot drift from the code: adding a
    field adds a variable here in the same commit.
    """

    sys.path.insert(0, str(REPO_ROOT))
    from app.config import Settings

    return {name.upper(): f"app/config.py:Settings.{name}" for name in Settings.model_fields}


_DIRECT_READ = re.compile(
    r"""os\.(?:environ(?:\.get|\.setdefault)?|getenv)\s*[(\[]\s*["']([A-Z][A-Z0-9_]{2,})["']"""
)


def direct_reads() -> dict[str, str]:
    """Variables read straight from ``os.environ``, bypassing ``Settings``.

    There is normally at most one of these and it is worth seeing: a variable
    read outside the model is invisible to ``Settings``, so nothing validates
    it, nothing documents it by construction, and it will not appear in
    ``/health`` or any startup log.
    """

    found: dict[str, str] = {}
    for root in ("app", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for match in _DIRECT_READ.finditer(_read(path)):
                rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
                found.setdefault(match.group(1), rel)
    return found


_NAIVE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")


def naive_grep_reads() -> set[str]:
    """What a text search WOULD have concluded, for the self-test only.

    Never used to decide anything. It exists so ``--check`` can demonstrate,
    on every run, that resolving the model finds variables the grep misses --
    the claim in this module's docstring, checked rather than asserted.
    """

    found: set[str] = set()
    for root in ("app", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            found.update(_NAIVE.findall(_read(path)))
    return found


# --- DOCUMENTED -------------------------------------------------------------

_DOC_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]{2,})=")


def documented_variables() -> set[str]:
    """Names ``.env.example`` declares, commented-out ones included."""

    out: set[str] = set()
    for line in _read(REPO_ROOT / ".env.example").split("\n"):
        match = _DOC_LINE.match(line.strip())
        if match:
            out.add(match.group(1))
    return out


# --- WIRED AT DEPLOY --------------------------------------------------------


@dataclass
class Wiring:
    """What ``cloudbuild.yaml`` sets on the revision."""

    #: NAME -> the literal or ``${_SUBSTITUTION}`` it is set from.
    env_vars: dict[str, str] = field(default_factory=dict)
    #: Secret Manager mounts, if any are ever added.
    secrets: dict[str, str] = field(default_factory=dict)
    #: Substitution keys the file declares, with their defaults.
    declared_substitutions: dict[str, str] = field(default_factory=dict)

    @property
    def hardcoded(self) -> dict[str, str]:
        """Variables set to a literal rather than a per-environment substitution.

        The section this inventory exists for. There is ONE ``cloudbuild.yaml``
        serving both environments, so a literal here is the same value in prep
        and in production -- and nothing in the file says which environment it
        is describing.

        ``AUTH_ENABLED=true`` is the example that earned this check: the
        programme's parity ledger records auth as disabled in both
        environments, that is true of agent-engine, and reading it as true of
        this service too is a mistake that costs a 403 on every write. A
        hardcoded value is not a defect -- it is a value with no environment
        dimension, and it should be visible as such rather than inferred from a
        184-character flag.
        """

        return {k: v for k, v in self.env_vars.items() if "${" not in v}


_SUBSTITUTION_BLOCK = re.compile(r"^substitutions:\n((?:  \S.*\n)+)", re.MULTILINE)
_SUBSTITUTION_LINE = re.compile(r"^  (_[A-Z][A-Z0-9_]*):\s*(.*)$")


def collect_wiring() -> Wiring:
    source = _read(REPO_ROOT / "cloudbuild.yaml")
    wiring = Wiring()

    block = _SUBSTITUTION_BLOCK.search(source)
    if block:
        for line in block.group(1).split("\n"):
            match = _SUBSTITUTION_LINE.match(line)
            if match:
                wiring.declared_substitutions[match.group(1)] = match.group(2).strip().strip('"')

    # `^|^` selects `|` as the delimiter instead of a comma, because
    # AUTH_ALLOWED_SERVICE_ACCOUNTS is a JSON array and a comma-delimited list
    # would split it mid-value. Parsing this as CSV is the obvious bug.
    for flag, target in (("set-env-vars", wiring.env_vars), ("set-secrets", wiring.secrets)):
        for raw in re.findall(rf'--{flag}=([^"\n]+)', source):
            body = raw
            delimiter = ","
            if body.startswith("^") and body[1:2] and body[2:3] == "^":
                delimiter = body[1]
                body = body[3:]
            for pair in body.split(delimiter):
                if "=" not in pair:
                    continue
                name, _, value = pair.partition("=")
                name = name.strip()
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
                    target[name] = value.strip()
    return wiring


# --- PARITY -----------------------------------------------------------------

_REPO_VAR = re.compile(r"vars\.(PREP|PROD)_([A-Z][A-Z0-9_]*)")
# The lookbehind is not decoration: without it, `COMMIT_SHA=$SHA` matches as a
# substitution named `_SHA`, and the report invents a hard failure about a key
# nobody wrote. A config checker that reports phantom problems gets muted, and a
# muted checker is worse than none.
_SUBSTITUTION_USE = re.compile(r"(?<![A-Za-z0-9_])(_[A-Z][A-Z0-9_]*)=")


@dataclass
class Parity:
    """prep against prod, by shape rather than by value.

    agent-middleware has no ``cloudbuild.promote.yaml``: production reuses the
    one ``cloudbuild.yaml`` with different substitutions supplied by
    ``deploy-prod.yml``. So parity here is not two files agreeing -- it is the
    two workflows passing the same SET of substitutions from correspondingly
    named repository variables.

    That much is checkable in CI with no credentials. What is NOT checkable
    here, and needs a human with gcloud, is whether each repository variable
    actually holds a value and whether the runtime service account holds the
    IAM bindings it needs. A missing binding fails at call time rather than at
    deploy time, so the deploy goes green and the capability is dead.
    """

    prep_suffixes: set[str] = field(default_factory=set)
    prod_suffixes: set[str] = field(default_factory=set)
    prep_substitutions: set[str] = field(default_factory=set)
    prod_substitutions: set[str] = field(default_factory=set)

    @property
    def prep_only(self) -> list[str]:
        return sorted(self.prep_suffixes - self.prod_suffixes)

    @property
    def prod_only(self) -> list[str]:
        return sorted(self.prod_suffixes - self.prep_suffixes)

    @property
    def substitution_gaps(self) -> list[str]:
        return sorted(self.prep_substitutions ^ self.prod_substitutions)


def collect_parity() -> Parity:
    parity = Parity()
    for env, filename in (("PREP", "deploy-prep.yml"), ("PROD", "deploy-prod.yml")):
        source = _read(REPO_ROOT / ".github" / "workflows" / filename)
        suffixes = {m.group(2) for m in _REPO_VAR.finditer(source) if m.group(1) == env}
        substitutions = set(_SUBSTITUTION_USE.findall(source))
        if env == "PREP":
            parity.prep_suffixes, parity.prep_substitutions = suffixes, substitutions
        else:
            parity.prod_suffixes, parity.prod_substitutions = suffixes, substitutions
    return parity


# --- the report -------------------------------------------------------------


@dataclass
class Inventory:
    read_by_code: dict[str, str]
    direct: dict[str, str]
    documented: set[str]
    wiring: Wiring
    parity: Parity

    @property
    def read_names(self) -> set[str]:
        return set(self.read_by_code) | set(self.direct)

    @property
    def read_but_undocumented(self) -> list[str]:
        return sorted(
            n
            for n in self.read_names
            if n not in self.documented and n not in NOT_APP_CONFIG
        )

    @property
    def wired_but_unread(self) -> list[str]:
        return sorted(n for n in self.wiring.env_vars if n not in self.read_names)

    @property
    def documented_but_unread(self) -> list[str]:
        return sorted(
            n for n in self.documented if n not in self.read_names and n not in NOT_APP_CONFIG
        )

    @property
    def substitutions_never_passed(self) -> list[str]:
        used = self.parity.prep_substitutions | self.parity.prod_substitutions
        return sorted(
            k
            for k in self.wiring.declared_substitutions
            if k not in used and k not in BUILD_ONLY_SUBSTITUTIONS
        )

    @property
    def substitutions_undeclared(self) -> list[str]:
        """Passed by a workflow, absent from ``substitutions:``.

        A hard failure, and the least obvious one in this file. Cloud Build
        rejects an unknown substitution key, so the deploy fails -- but it
        fails at build submit, minutes in, with a message about substitutions
        rather than about the variable somebody renamed.
        """

        used = self.parity.prep_substitutions | self.parity.prod_substitutions
        referenced = {
            m for m in re.findall(r"\$\{(_[A-Z][A-Z0-9_]*)\}", _read(REPO_ROOT / "cloudbuild.yaml"))
        }
        known = set(self.wiring.declared_substitutions) | referenced
        return sorted(k for k in used if k not in known and k != "COMMIT_SHA")


def build_inventory() -> Inventory:
    return Inventory(
        read_by_code=settings_variables(),
        direct=direct_reads(),
        documented=documented_variables(),
        wiring=collect_wiring(),
        parity=collect_parity(),
    )


def _print_report(inv: Inventory) -> None:
    w = inv.wiring
    print("\n=== CONFIG INVENTORY — agent-middleware ===\n")
    print(f"read by code (Settings fields):    {len(inv.read_by_code)}")
    print(f"read directly from os.environ:     {len(inv.direct)}")
    print(f"documented in .env.example:        {len(inv.documented)}")
    print(f"wired at deploy (env vars):        {len(w.env_vars)}")
    print(f"wired at deploy (secrets):         {len(w.secrets)}")

    print("\n--- HARDCODED AT DEPLOY: same value in prep AND prod ---")
    print("    One cloudbuild.yaml serves both environments, so these have no")
    print("    per-environment dimension. Read this section before assuming an")
    print("    environment behaves like a sibling service does.")
    for name, value in sorted(w.hardcoded.items()):
        print(f"  {name:<34} = {value}")
    if not w.hardcoded:
        print("  (none)")

    print(
        f"\n--- READ BY CODE, NOT DOCUMENTED ({len(inv.read_but_undocumented)})"
        " — hard failure ---"
    )
    for name in inv.read_but_undocumented:
        print(f"  {name:<34} {inv.read_by_code.get(name) or inv.direct.get(name)}")
    if not inv.read_but_undocumented:
        print("  (none)")

    print(f"\n--- READ OUTSIDE Settings ({len(inv.direct)}) — warning ---")
    for name, where in sorted(inv.direct.items()):
        print(f"  {name:<34} {where}")
    if not inv.direct:
        print("  (none)")

    print(
        f"\n--- WIRED, NOT READ ({len(inv.wired_but_unread)})"
        " — WARNING, never a deletion list ---"
    )
    print('    Settings uses extra="ignore", so an unread variable is silently')
    print("    tolerated. Each entry is either dead config or a read this")
    print("    inventory cannot see. A person decides which.")
    for name in inv.wired_but_unread:
        print(f"  {name}")
    if not inv.wired_but_unread:
        print("  (none)")

    print(f"\n--- DOCUMENTED, NOT READ ({len(inv.documented_but_unread)}) — warning ---")
    for name in inv.documented_but_unread:
        print(f"  {name}")
    if not inv.documented_but_unread:
        print("  (none)")

    print("\n--- PREP / PROD PARITY (shape, not values) ---")
    p = inv.parity
    print(f"  prep repository variables: {len(p.prep_suffixes)}")
    print(f"  prod repository variables: {len(p.prod_suffixes)}")
    print(f"  prep-only: {p.prep_only or '(none)'}")
    print(f"  prod-only: {p.prod_only or '(none)'}")
    print(f"  substitution set differs by: {p.substitution_gaps or '(none)'}")
    print("  NOT CHECKED HERE (needs gcloud): whether each variable holds a")
    print("  value, and whether the runtime service account holds its IAM")
    print("  bindings. A missing binding fails at call time, not at deploy.")

    print(
        f"\n--- SUBSTITUTIONS DECLARED, NEVER PASSED"
        f" ({len(inv.substitutions_never_passed)}) — warning ---"
    )
    for name in inv.substitutions_never_passed:
        print(f"  {name} (falls back to {w.declared_substitutions[name]!r})")
    if not inv.substitutions_never_passed:
        print("  (none)")

    print(
        f"\n--- SUBSTITUTIONS PASSED, NEVER DECLARED"
        f" ({len(inv.substitutions_undeclared)}) — hard failure ---"
    )
    for name in inv.substitutions_undeclared:
        print(f"  {name}")
    if not inv.substitutions_undeclared:
        print("  (none)")

    naive = naive_grep_reads()
    missed = sorted(n for n in inv.read_by_code if n not in naive)
    print("\n--- SELF-TEST: is resolving the model doing real work? ---")
    print(f"  variables a naive grep would MISS: {len(missed)} of {len(inv.read_by_code)}")
    if missed:
        print(f"  e.g. {', '.join(missed[:5])}")
    print()


def _run_checks(inv: Inventory) -> int:
    failed = False

    if inv.read_but_undocumented:
        print(
            f"\nconfig-inventory: {len(inv.read_but_undocumented)} variable(s) read by code and "
            "absent from .env.example:",
            file=sys.stderr,
        )
        for name in inv.read_but_undocumented:
            print(f"  {name}", file=sys.stderr)
        print(
            "\nDocument each with what it does, whether it is required, and WHAT HAPPENS WHEN IT "
            "IS ABSENT.",
            file=sys.stderr,
        )
        failed = True

    if inv.substitutions_undeclared:
        print(
            "\nconfig-inventory: workflow passes substitution(s) cloudbuild.yaml does not declare: "
            + ", ".join(inv.substitutions_undeclared),
            file=sys.stderr,
        )
        print(
            "Cloud Build rejects an unknown key, so this is a deploy that fails late.",
            file=sys.stderr,
        )
        failed = True

    gaps = inv.parity.prep_only + inv.parity.prod_only
    if gaps:
        print(
            "\nconfig-inventory: prep and prod are wired from different variable sets: "
            + ", ".join(gaps),
            file=sys.stderr,
        )
        print(
            "Everything validated against prep has to exist in production too (SCRUM-333). If a "
            "difference is deliberate, say so in the workflow rather than leaving it silent.",
            file=sys.stderr,
        )
        failed = True

    if inv.parity.substitution_gaps:
        print(
            "\nconfig-inventory: the two deploy workflows pass different substitutions: "
            + ", ".join(inv.parity.substitution_gaps),
            file=sys.stderr,
        )
        failed = True

    # The self-test. If the model-derived read set ever stops beating a plain
    # text search, the derivation has been replaced by something weaker and
    # every delta above is measured against the wrong baseline.
    naive = naive_grep_reads()
    if inv.read_by_code and not any(n not in naive for n in inv.read_by_code):
        print(
            "\nconfig-inventory: SELF-TEST FAILED — a naive grep found every variable the model "
            "declares, which means the read set is no longer being derived from Settings. Fix "
            "this script; do not touch the config.",
            file=sys.stderr,
        )
        failed = True

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="CI mode: exit 1 on a hard delta")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    inv = build_inventory()

    if args.json:
        payload: dict[str, Any] = {
            "readByCode": sorted(inv.read_by_code),
            "readDirectly": sorted(inv.direct),
            "documented": sorted(inv.documented),
            "wiredEnvVars": dict(sorted(inv.wiring.env_vars.items())),
            "wiredSecrets": dict(sorted(inv.wiring.secrets.items())),
            "hardcoded": dict(sorted(inv.wiring.hardcoded.items())),
            "deltas": {
                "readButUndocumented": inv.read_but_undocumented,
                "wiredButUnread": inv.wired_but_unread,
                "documentedButUnread": inv.documented_but_unread,
                "substitutionsNeverPassed": inv.substitutions_never_passed,
                "substitutionsUndeclared": inv.substitutions_undeclared,
            },
            "parity": {
                "prepOnly": inv.parity.prep_only,
                "prodOnly": inv.parity.prod_only,
                "substitutionGaps": inv.parity.substitution_gaps,
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_report(inv)

    return _run_checks(inv) if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
