"""L2 M4 (SCRUM-346): the half of the cost table the repo can actually produce.

The cases here are mostly about what the script REFUSES to do. A cost table is
the kind of artefact people act on without re-deriving, so the ways it can be
quietly wrong matter more than the ways it can be right: a defaulted price, a
collapsed distinction between "deliberately unpriced" and "unknown", or a
heuristic presented as a finding would each produce a table that reads as
authoritative and is not.
"""

from __future__ import annotations

import json

import pytest

from scripts.step_cost_inventory import (
    build,
    classify,
    load_catalog,
    model_steps,
    priced,
)


class TestWhichStepsCostAnything:
    def test_only_model_running_steps_are_counted(self) -> None:
        # `code` and `gate` steps cost nothing per token. A gate costs a
        # person's time, which is M8's subject rather than M4's.
        stages = {
            "an-agent": [
                {"id": "01-load", "kind": "code"},
                {"id": "02-draft", "kind": "agent", "default_model": "claude-sonnet-4-6"},
                {"id": "03-review", "kind": "gate"},
            ]
        }
        rows = model_steps(stages)
        assert [r["step"] for r in rows] == ["02-draft"]

    def test_carries_the_agent_id_and_skill_ref_a_reader_needs_to_act(self) -> None:
        # Without these the table names a step nobody can find: the Studio keys
        # a per-stage override on `agent_id`, and the prompt lives at `skillRef`.
        stages = {
            "x-agent": [
                {
                    "id": "10-draft-post",
                    "kind": "agent",
                    "agent_id": "x-draft",
                    "skill_ref": "x-craft@7",
                    "default_model": "claude-sonnet-4-6",
                    "vendor": "anthropic",
                }
            ]
        }
        assert model_steps(stages)[0] == {
            "agent": "x-agent",
            "step": "10-draft-post",
            "agentId": "x-draft",
            "skillRef": "x-craft@7",
            "model": "claude-sonnet-4-6",
            "vendor": "anthropic",
            "work": "creative",
        }


class TestTheThreePriceStates:
    """The distinction this file exists to protect."""

    def test_a_priced_model_carries_its_real_rate(self) -> None:
        rows = priced(
            [{"agent": "a", "step": "s", "model": "claude-sonnet-4-6", "work": "creative"}],
            load_catalog(),
        )
        assert rows[0]["priceState"] == "priced"
        assert rows[0]["inputPer1M"] == 3.0
        assert rows[0]["outputPer1M"] == 15.0

    def test_a_deliberately_unpriced_model_is_NOT_the_same_as_an_unknown_one(self) -> None:
        # THE CASE THIS SPLIT EXISTS FOR. `UNPRICED` is a model somebody looked
        # at and recorded as unpriceable from a primary source — a decision.
        # A model in neither list is a gap nobody knows about. Collapsing them
        # would bury the second under the first, which is exactly how a
        # deliberate exception grows into an unnoticed hole.
        catalog = load_catalog()
        known = priced(
            [{"agent": "a", "step": "s", "model": "gemini-3.1-pro-preview", "work": "x"}], catalog
        )
        unknown = priced(
            [{"agent": "a", "step": "s", "model": "a-model-nobody-added", "work": "x"}], catalog
        )
        assert known[0]["priceState"] == "unpriced-on-purpose"
        assert unknown[0]["priceState"] == "absent"

    def test_an_unpriced_step_is_never_given_a_plausible_default(self) -> None:
        # Defaulting to Sonnet's $3/$15 would make every one of these rows look
        # answered, and a reader would never learn the price is not known.
        rows = priced(
            [{"agent": "a", "step": "s", "model": "a-model-nobody-added", "work": "x"}],
            load_catalog(),
        )
        assert rows[0]["inputPer1M"] is None
        assert rows[0]["blendedPer1M"] is None

    def test_joins_on_the_provider_name_as_well_as_our_own_id(self) -> None:
        # A stage's `default_model` is what the engine compiled — a PROVIDER
        # name (`claude-sonnet-4-6`) — while the catalog is keyed on our id
        # (`claude-sonnet-4-6-on-vertex`). Joining on one of them alone drops
        # most of the fleet and every dropped row looks like a missing price.
        catalog = load_catalog()
        assert "claude-sonnet-4-6" in catalog
        assert "claude-sonnet-4-6-on-vertex" in catalog


class TestTheMechanicalGuess:
    @pytest.mark.parametrize(
        "step_id", ["12-verify-brand-compliance", "guardrail-verify", "04b3-extract-entities"]
    )
    def test_a_step_whose_output_is_a_verdict_is_mechanical(self, step_id: str) -> None:
        assert classify(step_id) == "mechanical"

    @pytest.mark.parametrize("step_id", ["10-draft-post", "00c3-write-design-brief"])
    def test_a_step_whose_output_the_client_reads_is_creative(self, step_id: str) -> None:
        assert classify(step_id) == "creative"

    def test_a_CHECK_on_creative_work_is_still_a_check(self) -> None:
        # `verify-headline` produces a verdict about a headline, not a headline.
        # Mechanical is tested first for exactly this.
        assert classify("11-verify-headline") == "mechanical"

    def test_a_step_matching_neither_is_left_unclassified_rather_than_defaulted(self) -> None:
        # A wrong default here becomes a model swap nobody argued for.
        assert classify("03a-moment") == "unclassified"


class TestWhatTheTableSaysAboutItself:
    def test_states_that_it_is_a_RATE_table_and_not_a_spend_table(self) -> None:
        # The one sentence that stops this being mistaken for M4's answer. A
        # reader who takes the ordering for a spend ranking will retire the
        # wrong steps.
        _, table = build()
        assert "rate table, not a spend table" in table
        assert "live only in run telemetry" in table

    def test_labels_the_mechanical_list_as_a_guess(self) -> None:
        _, table = build()
        assert "**guess**" in table
        assert "a list to argue with" in table

    def test_reports_the_real_fleet_rather_than_a_sample(self) -> None:
        rows, _ = build()
        assert len({r["agent"] for r in rows}) == 16
        assert len(rows) > 50

    def test_json_mode_carries_the_price_state_so_a_consumer_can_filter(self) -> None:
        rows, _ = build()
        assert {r["priceState"] for r in rows} <= {"priced", "unpriced-on-purpose", "absent"}
        assert json.dumps(rows)  # serialisable, so M3 can join against it
