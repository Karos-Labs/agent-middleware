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

    def test_the_parser_does_not_answer_none_when_it_means_cannot_see(self) -> None:
        """The regression this test exists for actually happened.

        The deploy step used to be a flat `args` list and the parser matched
        `--set-env-vars=...` in it. When the step became a shell script -- so
        the Cloud SQL flags could be PRESENT or ABSENT rather than empty --
        that regex stopped matching and this inventory reported *zero* wired
        variables, exit 0, no warning. "Nothing is wired" and "I cannot see
        what is wired" are opposite facts and the report gave the first.

        So: a floor, not an exact list. Every variable the deploy sets has to
        come back, and a parser that goes blind again trips this rather than
        going quiet.
        """

        env_vars = collect_wiring().env_vars
        for expected in (
            "ENVIRONMENT",
            "LOG_LEVEL",
            "GCP_PROJECT_ID",
            "PUBSUB_JOB_TOPIC_ID",
            "FIRESTORE_PROJECT_ID",
            "FIRESTORE_DATABASE",
            "GCS_ARTIFACTS_BUCKET",
            "AUTH_ENABLED",
            "AUTH_AUDIENCE",
            "AUTH_ALLOWED_SERVICE_ACCOUNTS",
            "CONFIG_DB_DSN",
        ):
            assert expected in env_vars, f"the deploy sets {expected} and the parser missed it"

    def test_a_substituted_value_is_not_mistaken_for_a_literal(self) -> None:
        """The shell indirection must not turn substitutions into hardcodings.

        In the shell form a value reads `$${gcp_project}`, not
        `${_GCP_PROJECT_ID}`. Left unresolved it contains no `${`, so the
        parity section would report every per-environment variable as
        hardcoded in both -- the exact opposite of the truth, and the section
        exists to answer that one question.
        """

        wiring = collect_wiring()
        assert "${_GCP_PROJECT_ID}" in wiring.env_vars["GCP_PROJECT_ID"]
        assert "GCP_PROJECT_ID" not in wiring.hardcoded
        # AUTH_ENABLED really is a literal in both environments.
        assert wiring.hardcoded.get("AUTH_ENABLED") == "true"


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


# --- The deploy step's own logic -------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent

_SUBSTITUTIONS = {
    "_SERVICE": "agent-middleware",
    "_REGION": "us-central1",
    "_REPO": "karos",
    "_SERVICE_ACCOUNT": "agent-middleware-sa@karoscmo-prep.iam.gserviceaccount.com",
    "_ENVIRONMENT": "staging",
    "_GCP_PROJECT_ID": "karoscmo-prep",
    "_PUBSUB_JOB_TOPIC_ID": "karos-agent-runs-prep",
    "_FIRESTORE_PROJECT_ID": "karoscmo",
    "_FIRESTORE_DATABASE": "prep",
    "_GCS_ARTIFACTS_BUCKET": "karoscmo-prep-agent-artifacts",
    "_AUTH_AUDIENCE": "https://agent-middleware-x.run.app",
    # JSON, with double quotes of its own. The reason this test exists.
    "_AUTH_ALLOWED_SERVICE_ACCOUNTS": '["portal@karoscmo-prep.iam.gserviceaccount.com","x@y.com"]',
    "PROJECT_ID": "karoscmo-prep",
    "COMMIT_SHA": "abc1234",
}

_PREP_DSN = (
    "postgresql://agent-middleware-sa%40karoscmo-prep.iam@/karos-prep"
    "?host=/cloudsql/karoscmo-prep:us-central1:karos-config-prep"
)
_PREP_INSTANCE = "karoscmo-prep:us-central1:karos-config-prep"


def _deploy_script() -> str:
    """The deploy step's shell body, dedented.

    Parsed by hand rather than with PyYAML: this repository does not depend on
    PyYAML, and a test that needs a package CI does not install is a test that
    reports as an error rather than as a failure.
    """

    source = (REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    marker = "      - -c\n      - |\n"
    assert marker in source, "the deploy step is no longer a `bash -c` script"
    body = source[source.index(marker) + len(marker) :]
    lines: list[str] = []
    for line in body.split("\n"):
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line[8:])
    return "\n".join(lines)


def _render(dsn: str, instance: str) -> str:
    """Cloud Build's substitution pass, done here the way it is done there.

    Textual, before any shell sees it -- which is precisely why the script
    captures every value in single quotes first.
    """

    script = _deploy_script()
    values = dict(_SUBSTITUTIONS, _CONFIG_DB_DSN=dsn, _CLOUDSQL_INSTANCE=instance)
    for name, value in values.items():
        script = script.replace("${" + name + "}", value).replace("$" + name, value)
    # Cloud Build's escape for a literal `$`.
    return script.replace("$$", "$")


def _run(dsn: str, instance: str, tmp_path: Path) -> tuple[int, list[str], str]:
    """Run the rendered script with a `gcloud` that only echoes its arguments."""

    import os
    import subprocess

    fake = tmp_path / "gcloud"
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", _render(dsn, instance)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    return result.returncode, result.stdout.splitlines(), result.stderr


class TestTheDeployStepRefusesHalfAConfiguration:
    """The three states of the configuration plane, at the deploy boundary.

    Both empty is a real deployment and has to keep working -- S1 has not
    landed Cloud SQL everywhere, and a deploy that started failing because a
    variable is unset would make the Postgres migration a flag day for routes
    that have nothing to do with it.

    Both set is the wired deployment.

    One set is the state worth a hard failure. A DSN with no attached instance
    points at `/cloudsql/...` in a container that has no such socket: the
    revision goes green, every route works, and only the configuration plane
    is dark. That is the failure mode this whole ticket family exists to stop
    shipping, so it is refused at deploy time rather than logged at runtime.
    """

    def test_neither_set_deploys_and_clears_the_connection(self, tmp_path: Path) -> None:
        code, args, _ = _run("", "", tmp_path)

        assert code == 0
        assert "--clear-cloudsql-instances" in args
        # Not "not present": present-and-empty is a configured blank string,
        # and Settings cannot tell that from a deliberate value.
        assert not any(a.startswith("--set-env-vars") and "CONFIG_DB_DSN" in a for a in args)

    def test_both_set_attaches_the_instance_and_passes_the_dsn(self, tmp_path: Path) -> None:
        code, args, _ = _run(_PREP_DSN, _PREP_INSTANCE, tmp_path)

        assert code == 0
        assert f"--set-cloudsql-instances={_PREP_INSTANCE}" in args
        (env_flag,) = [a for a in args if a.startswith("--set-env-vars")]
        assert f"CONFIG_DB_DSN={_PREP_DSN}" in env_flag

    @pytest.mark.parametrize(
        "dsn,instance",
        [(_PREP_DSN, ""), ("", _PREP_INSTANCE)],
        ids=["dsn-without-instance", "instance-without-dsn"],
    )
    def test_exactly_one_set_is_refused(self, dsn: str, instance: str, tmp_path: Path) -> None:
        code, _, stderr = _run(dsn, instance, tmp_path)

        assert code != 0
        assert "refusing to deploy" in stderr

    def test_the_json_allow_list_survives_the_shell(self, tmp_path: Path) -> None:
        """The bug this file caught before it shipped.

        Moving the deploy from a flat `args` list to a shell script put a
        JSON array carrying double quotes inside a double-quoted shell string.
        The quotes ended the string early and the allow-list arrived as
        `[a@x.com,b@y.com]` -- valid-looking, unparseable as JSON, and it would
        have refused every caller in production while the deploy went green.

        The fix is that every substitution is captured in SINGLE quotes first.
        This asserts the outcome rather than the technique.
        """

        _, args, _ = _run("", "", tmp_path)
        (env_flag,) = [a for a in args if a.startswith("--set-env-vars")]

        assert (
            "AUTH_ALLOWED_SERVICE_ACCOUNTS="
            '["portal@karoscmo-prep.iam.gserviceaccount.com","x@y.com"]' in env_flag
        )

    def test_the_pipe_delimiter_is_declared_before_the_first_value(self, tmp_path: Path) -> None:
        """`^|^` and nothing else, or the JSON array splits on its own comma."""

        _, args, _ = _run("", "", tmp_path)
        (env_flag,) = [a for a in args if a.startswith("--set-env-vars")]

        assert env_flag.startswith("--set-env-vars=^|^")
