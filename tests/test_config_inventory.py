"""The config inventory, and specifically the parts that could lie.

A config checker earns its place only if it fails when the config is wrong and
stays quiet when it is right. Both halves are worth testing, and the second is
the one people forget: a checker that reports phantom problems gets muted, and a
muted checker is worse than not having one.

The parsers are what these exercise. The report is print statements over them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.config_inventory import (
    Inventory,
    Parity,
    Wiring,
    build_inventory,
    collect_parity,
    collect_wiring,
    direct_reads,
    naive_grep_reads,
    settings_variables,
)


class TestReadSetIsDerivedNotGrepped:
    def test_every_settings_field_is_a_variable(self) -> None:
        # The authoritative read set. It cannot drift from the code, because
        # adding a field adds a variable here in the same commit.
        from app.config import Settings

        assert set(settings_variables()) == {n.upper() for n in Settings.model_fields}

    def test_a_naive_grep_would_miss_variables_the_model_declares(self) -> None:
        """The claim the whole approach rests on, checked rather than asserted.

        pydantic-settings resolves `gcp_project_id` from GCP_PROJECT_ID without
        the string appearing anywhere, so text search cannot see it. If this
        ever passes trivially, the read set has stopped being derived from
        Settings and every delta in the report is measured against the wrong
        baseline.
        """

        derived = set(settings_variables())
        missed = derived - naive_grep_reads()
        assert missed, "resolving the model found nothing a grep would have missed"

    def test_reads_that_bypass_settings_are_surfaced(self) -> None:
        # A variable read straight from os.environ is invisible to Settings:
        # nothing validates it and nothing documents it by construction. Worth
        # seeing even when it is legitimate.
        assert "FIRESTORE_EMULATOR_HOST" in direct_reads()


class TestDeployWiringParser:
    def test_parses_the_pipe_delimiter_rather_than_assuming_csv(self) -> None:
        """`^|^` selects `|` because AUTH_ALLOWED_SERVICE_ACCOUNTS is JSON.

        Parsing this as CSV is the obvious bug: the value would split mid-array
        and the last variable would come out named `"editor"]` or similar.
        """

        wiring = collect_wiring()
        assert "AUTH_ALLOWED_SERVICE_ACCOUNTS" in wiring.env_vars
        assert "GCP_PROJECT_ID" in wiring.env_vars
        # No name may contain a bracket or quote — that is what a mis-split
        # looks like, and it would otherwise pass silently.
        for name in wiring.env_vars:
            assert name.replace("_", "").isalnum(), f"{name!r} looks like a mis-split"

    def test_hardcoded_values_are_separated_from_substituted_ones(self) -> None:
        """The check that earned this whole ticket.

        AUTH_ENABLED is set to a literal `true` in the one cloudbuild.yaml that
        serves both environments. Reading the programme's parity ledger — which
        records auth as disabled, truthfully, about agent-engine — and assuming
        it holds here costs a 403 on every write in production. A value with no
        environment dimension should be visible as such, not inferred from a
        184-character flag.
        """

        hardcoded = collect_wiring().hardcoded
        assert hardcoded.get("AUTH_ENABLED") == "true"
        assert all("${" not in v for v in hardcoded.values())

    def test_substitutions_are_read_with_their_defaults(self) -> None:
        declared = collect_wiring().declared_substitutions
        assert "_GCP_PROJECT_ID" in declared
        assert declared["_REGION"] == "us-central1"


class TestParity:
    def test_prep_and_prod_are_wired_from_the_same_shape(self) -> None:
        # Not values — names. Everything validated against prep has to exist in
        # production too (SCRUM-333), and this is the half of that checkable
        # without credentials.
        parity = collect_parity()
        assert parity.prep_suffixes
        assert parity.prep_only == []
        assert parity.prod_only == []

    def test_both_workflows_pass_the_same_substitutions(self) -> None:
        assert collect_parity().substitution_gaps == []

    def test_commit_sha_is_not_mistaken_for_a_substitution_named_sha(self) -> None:
        """The false positive this parser had on its first run.

        `COMMIT_SHA=$SHA` matched a pattern looking for `_NAME=` and produced a
        hard failure about a substitution called `_SHA`. Guarding it is why the
        pattern has a lookbehind.
        """

        parity = collect_parity()
        assert "_SHA" not in parity.prep_substitutions | parity.prod_substitutions


class TestThisRepositoryIsClean:
    """The state AU52 asks for, asserted so it cannot silently regress."""

    def test_every_variable_the_code_reads_is_documented(self) -> None:
        inv = build_inventory()
        assert inv.read_but_undocumented == []

    def test_nothing_is_wired_that_no_code_reads(self) -> None:
        # The ratio AU52 names as the realistic target: karosCMO carries
        # exactly one piece of garbage. This repository currently carries none.
        assert build_inventory().wired_but_unread == []

    def test_nothing_documented_is_dead(self) -> None:
        assert build_inventory().documented_but_unread == []


class TestDeltaLogic:
    """The delta arithmetic, on constructed inputs rather than the live repo.

    The tests above prove this repository is clean, which means they would all
    still pass if the comparisons were broken. These fail if they are.
    """

    @staticmethod
    def _inventory(**over: object) -> Inventory:
        base: dict[str, object] = {
            "read_by_code": {"ALPHA": "app/config.py:Settings.alpha"},
            "direct": {},
            "documented": {"ALPHA"},
            "wiring": Wiring(env_vars={"ALPHA": "${_ALPHA}"}),
            "parity": Parity(),
        }
        base.update(over)
        return Inventory(**base)  # type: ignore[arg-type]

    def test_an_undocumented_read_is_reported(self) -> None:
        inv = self._inventory(read_by_code={"ALPHA": "x", "BETA": "y"})
        assert inv.read_but_undocumented == ["BETA"]

    def test_a_wired_variable_nothing_reads_is_reported(self) -> None:
        inv = self._inventory(wiring=Wiring(env_vars={"ALPHA": "${_A}", "GHOST": "1"}))
        assert inv.wired_but_unread == ["GHOST"]

    def test_platform_variables_are_not_treated_as_app_config(self) -> None:
        # Requiring HOME in .env.example would be noise that trains people to
        # skim the file, which is how a real omission gets missed.
        inv = self._inventory(read_by_code={"ALPHA": "x", "HOME": "y"}, documented={"ALPHA"})
        assert inv.read_but_undocumented == []

    def test_a_parity_gap_in_either_direction_is_reported(self) -> None:
        parity = Parity(prep_suffixes={"A", "B"}, prod_suffixes={"A", "C"})
        assert parity.prep_only == ["B"]
        assert parity.prod_only == ["C"]


def test_the_script_is_executable_and_exits_zero_on_a_clean_repo() -> None:
    """End to end, because --check is what CI runs and nothing else covers it."""

    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "scripts/config_inventory.py", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_json_output_is_parseable() -> None:
    import json
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "scripts/config_inventory.py", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "deltas" in payload and "parity" in payload
    assert payload["hardcoded"]["AUTH_ENABLED"] == "true"


@pytest.mark.parametrize("flag", ["--check", "--json"])
def test_both_modes_are_wired(flag: str) -> None:
    assert flag in Path("scripts/config_inventory.py").read_text(encoding="utf-8")
