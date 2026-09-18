"""N4 (SCRUM-487): which post goes in each slot, and why.

Every rule in the ticket is a countable property of the finished plan, so every
rule here is a case that counts it. The cases worth reading twice are the ones
where two rules disagree -- a client request against the variety rules, a
promotional row against the six-post cap, a timely row with no news behind it --
because those are the places a sequencer quietly stops being the thing it was
described as.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.services.sequencing import (
    DEFAULT_MIX,
    PROMOTIONAL,
    TIMELY,
    history,
    mix_target,
    open_rows,
    plan,
    promotional_allowed,
    sequences_product,
    stage_deficit,
)


def row(idea: str, stage: str, **patch) -> dict:
    return {"id": f"row-{idea}", "idea": idea, "stage": stage, "problem": "p", **patch}


def posted(stage: str, type_: str | None = None, status: str = "posted") -> dict:
    return {"subject": f"s-{stage}", "stage": stage, "type": type_, "status": status}


def a_map(*rows: dict) -> list[dict]:
    return list(rows)


# A pool deep enough that no case runs out of rows by accident: four of each.
DEEP = a_map(
    *[row(f"{stage}{i}", stage) for stage in ("attention", "expertise", "decide") for i in range(4)]
)


class TestWhatCountsAsACandidate:
    def test_used_and_retired_rows_are_history_not_candidates(self) -> None:
        rows = a_map(
            row("a", "attention"),
            row("b", "attention", status="used"),
            row("c", "attention", status="retired"),
        )
        assert [r["idea"] for r in open_rows(rows)] == ["a"]

    def test_a_row_with_an_off_funnel_stage_is_dropped_not_defaulted(self) -> None:
        # Defaulting it would put a post in the plan under a stage nobody chose
        # and distort the very mix this module exists to hold.
        assert open_rows(a_map(row("a", "consideration"))) == []

    def test_a_row_with_no_idea_is_not_a_post(self) -> None:
        assert open_rows(a_map(row("   ", "attention"))) == []


class TestHistoryIsReadOldestFirst:
    def test_reverses_the_newest_first_window(self) -> None:
        # `subject_window` serves newest-first. Every rule here is about what
        # follows what, so the direction is load-bearing.
        window = [posted("decide"), posted("expertise"), posted("attention")]
        assert [h["stage"] for h in history(window)] == ["attention", "expertise", "decide"]

    def test_a_skipped_draft_is_not_something_the_audience_saw(self) -> None:
        window = [posted("attention", status="skipped"), posted("decide")]
        assert [h["stage"] for h in history(window)] == ["decide"]


class TestTheMixIsAShareNotARotation:
    def test_falls_back_to_D32_when_the_client_has_no_mix(self) -> None:
        assert mix_target(None) == DEFAULT_MIX
        assert mix_target({"nonsense": 4}) == DEFAULT_MIX

    def test_an_empty_history_owes_every_stage_its_full_share(self) -> None:
        owed = stage_deficit(Counter(), DEFAULT_MIX)
        assert owed[0][1] == "attention"  # the largest share opens

    def test_the_stage_just_used_stops_being_the_most_owed(self) -> None:
        owed = stage_deficit(Counter({"attention": 1}), DEFAULT_MIX)
        assert owed[0][1] != "attention"

    def test_six_slots_land_on_the_mix_rather_than_on_a_rotation(self) -> None:
        # THE CASE THIS MODULE EXISTS FOR. A rotation would give 2/2/2; the mix
        # says 3/2/1, and over six posts the deficit rule has to actually reach
        # it rather than merely avoid repeats.
        result = plan(slots=6, map_rows=DEEP, recent=[])
        stages = Counter(slot["stage"] for slot in result["slots"])
        assert stages == {"attention": 3, "expertise": 2, "decide": 1}

    def test_a_client_mix_is_honoured_over_the_default(self) -> None:
        result = plan(slots=4, map_rows=DEEP, recent=[], default_mix={"attention": 1, "decide": 1})
        stages = Counter(slot["stage"] for slot in result["slots"])
        assert stages == {"attention": 2, "decide": 2}
        assert "expertise" not in stages


class TestNeverTwiceInARow:
    def test_the_first_slot_does_not_repeat_what_went_out_LAST(self) -> None:
        # The case a client notices first: the plan looks varied inside itself
        # and opens with a second helping of yesterday.
        #
        # The history is chosen so the MIX and the VARIETY rule disagree. After
        # one attention and two expertise posts, attention is the stage the mix
        # owes most — and it is also the one that just went out. A test whose
        # history left the two rules agreeing would pass with the variety rule
        # deleted, which is how this case read the first time it was written.
        window = [posted("attention"), posted("expertise"), posted("expertise")]
        assert stage_deficit(Counter({"attention": 1, "expertise": 2}), DEFAULT_MIX)[0][1] == (
            "attention"
        ), "the premise: the mix wants attention next"
        assert plan(slots=1, map_rows=DEEP, recent=window)["slots"][0]["stage"] != "attention"

    def test_the_first_slot_does_not_repeat_the_TYPE_that_went_out_last_either(self) -> None:
        # Same shape as the case above, one rule down: the stage rule is
        # satisfied either way, so only the type rule can decide this.
        rows = a_map(row("a1", "attention", type="story"), row("a2", "attention", type="teardown"))
        window = [posted("expertise", "story"), posted("attention"), posted("attention")]
        assert plan(slots=1, map_rows=rows, recent=window)["slots"][0]["type"] == "teardown"

    def test_no_two_consecutive_slots_share_a_stage(self) -> None:
        stages = [slot["stage"] for slot in plan(slots=6, map_rows=DEEP, recent=[])["slots"]]
        assert all(a != b for a, b in zip(stages, stages[1:], strict=False))

    def test_no_two_consecutive_slots_share_a_type(self) -> None:
        rows = a_map(
            row("a1", "attention", type="story"),
            row("e1", "expertise", type="story"),
            row("e2", "expertise", type="teardown"),
            row("d1", "decide", type="case-study"),
        )
        types = [slot.get("type") for slot in plan(slots=3, map_rows=rows, recent=[])["slots"]]
        assert types[0] == "story" and types[1] != "story"

    def test_rows_with_no_type_do_not_block_each_other(self) -> None:
        # Two untyped rows are not "the same type twice"; treating `None` as a
        # value would make an unlabelled map unplannable.
        result = plan(slots=3, map_rows=DEEP, recent=[])
        assert len(result["slots"]) == 3


class TestPromotionalAtMostOneInSix:
    def test_allows_the_first_one(self) -> None:
        assert promotional_allowed([None, None, None])

    def test_refuses_a_second_inside_the_window(self) -> None:
        assert not promotional_allowed([PROMOTIONAL, None, None, None])

    def test_the_window_is_six_CONSECUTIVE_posts_not_a_calendar_week(self) -> None:
        # Five ordinary posts after a promotional one, and the sixth may be
        # promotional again. One fewer and it may not.
        assert promotional_allowed([PROMOTIONAL, None, None, None, None, None])
        assert not promotional_allowed([PROMOTIONAL, None, None, None, None])

    def test_a_plan_never_puts_two_promotional_posts_in_six(self) -> None:
        rows = a_map(
            *[
                row(f"p{i}", stage, type=PROMOTIONAL)
                for i, stage in enumerate(("attention", "expertise", "decide"))
            ],
            *[row(f"o{i}", stage) for i, stage in enumerate(("attention", "expertise", "decide"))],
            *[row(f"q{i}", stage) for i, stage in enumerate(("attention", "expertise", "decide"))],
        )
        types = [slot.get("type") for slot in plan(slots=6, map_rows=rows, recent=[])["slots"]]
        assert types.count(PROMOTIONAL) <= 1

    def test_a_promotional_post_in_the_HISTORY_spends_the_allowance(self) -> None:
        rows = a_map(row("p", "expertise", type=PROMOTIONAL), row("o", "expertise"))
        result = plan(slots=1, map_rows=rows, recent=[posted("attention", PROMOTIONAL)])
        assert result["slots"][0].get("type") != PROMOTIONAL


class TestTimelyNeedsARealAnchor:
    TIMELY_MAP = a_map(row("news", "attention", type=TIMELY), row("ever", "attention"))

    def test_no_anchor_no_timely_post(self) -> None:
        result = plan(slots=1, map_rows=self.TIMELY_MAP, recent=[])
        assert result["slots"][0]["subject"] == "ever"

    def test_an_anchor_lets_the_timely_row_run_and_becomes_its_why_now(self) -> None:
        result = plan(
            slots=1,
            map_rows=self.TIMELY_MAP,
            recent=[],
            anchors=[{"whyNow": "the rules changed on Tuesday"}],
        )
        assert result["slots"][0]["subject"] == "news"
        assert result["slots"][0]["whyNow"] == "the rules changed on Tuesday"

    def test_one_anchor_cannot_justify_two_timely_posts(self) -> None:
        rows = a_map(
            row("n1", "attention", type=TIMELY),
            row("n2", "expertise", type=TIMELY),
            row("n3", "decide", type=TIMELY),
            row("e1", "expertise"),
            row("d1", "decide"),
        )
        subjects = [
            slot["subject"]
            for slot in plan(slots=3, map_rows=rows, recent=[], anchors=[{"whyNow": "one thing"}])[
                "slots"
            ]
        ]
        assert subjects.count("n2") + subjects.count("n3") + subjects.count("n1") == 1

    def test_an_ordinary_slot_gets_NO_why_now_rather_than_an_invented_one(self) -> None:
        # A fabricated "why now" goes on the client's card as though somebody
        # meant it. Absent is the honest answer.
        assert "whyNow" not in plan(slots=1, map_rows=DEEP, recent=[])["slots"][0]


class TestAClientRequestWins:
    def test_it_takes_the_next_slot(self) -> None:
        result = plan(
            slots=2, map_rows=DEEP, recent=[], requests=[{"subject": "our funding round"}]
        )
        assert result["slots"][0]["subject"] == "our funding round"
        assert result["slots"][0]["source"] == "client-request"

    def test_it_wins_even_when_it_REPEATS_the_last_stage(self) -> None:
        # The case that decides what "wins" means. Holding a client's own
        # request back a slot to preserve a pattern is the portal overruling the
        # person paying for the post.
        result = plan(
            slots=1,
            map_rows=DEEP,
            recent=[posted("decide")],
            requests=[{"subject": "our funding round", "stage": "decide"}],
        )
        assert result["slots"][0]["stage"] == "decide"
        assert "wins over the mix" in result["slots"][0]["reason"]

    def test_a_request_with_no_stage_is_filed_where_the_mix_is_most_owed(self) -> None:
        result = plan(slots=1, map_rows=DEEP, recent=[], requests=[{"subject": "x"}])
        assert result["slots"][0]["stage"] == "attention"

    def test_a_request_with_no_subject_is_not_a_request(self) -> None:
        result = plan(slots=1, map_rows=DEEP, recent=[], requests=[{"whyNow": "please"}])
        assert result["slots"][0]["source"] == "strategy-map"

    def test_several_requests_are_taken_in_order_and_then_the_map_resumes(self) -> None:
        result = plan(
            slots=3,
            map_rows=DEEP,
            recent=[],
            requests=[{"subject": "first"}, {"subject": "second"}],
        )
        assert [slot["subject"] for slot in result["slots"][:2]] == ["first", "second"]
        assert result["slots"][2]["source"] == "strategy-map"


class TestPerformanceIsNotFakedWhileItDoesNotExist:
    def test_says_so_in_the_reason_and_in_the_notes(self) -> None:
        result = plan(slots=1, map_rows=DEEP, recent=[])
        assert "no performance data" in result["slots"][0]["reason"]
        assert any("what-works" in note for note in result["notes"])

    def test_given_a_performance_map_the_best_row_of_the_stage_goes_first(self) -> None:
        rows = a_map(row("dull", "attention"), row("great", "attention"))
        result = plan(slots=1, map_rows=rows, recent=[], performance={"row-great": 9.0})
        assert result["slots"][0]["subject"] == "great"
        assert "by performance" in result["slots"][0]["reason"]


class TestAnExhaustedMapIsSaidOutLoud:
    def test_reports_how_many_slots_it_could_not_fill(self) -> None:
        # A short plan and an empty pool look identical to a caller that reads
        # only the list, and the difference is whether the client is about to
        # start repeating themselves.
        result = plan(
            slots=4, map_rows=a_map(row("a", "attention"), row("e", "expertise")), recent=[]
        )
        assert len(result["slots"]) == 2
        assert result["unfilled"] == 2
        assert any("rebuilding" in note for note in result["notes"])

    def test_an_empty_map_plans_nothing_rather_than_failing(self) -> None:
        result = plan(slots=3, map_rows=[], recent=[])
        assert result["slots"] == [] and result["unfilled"] == 3

    def test_zero_slots_is_a_plan_with_no_slots(self) -> None:
        assert plan(slots=0, map_rows=DEEP, recent=[])["slots"] == []


class TestTheTwoProductsThisDoesNotSpeakFor:
    @pytest.mark.parametrize("product", ["tiktok-editing-agent", "branded-shorts-agent"])
    def test_tiktok_editing_is_on_demand_and_has_no_sequence(self, product: str) -> None:
        # D19. It shares the TikTok platform, and therefore the subject history,
        # with two products that DO have slots — so the check has to be on the
        # product, not the platform.
        assert sequences_product(product) is False

    @pytest.mark.parametrize(
        "product", ["x-agent", "linkedin-agent", "instagram-agent", "tiktok-clipping-agent"]
    )
    def test_everything_with_a_calendar_does(self, product: str) -> None:
        assert sequences_product(product) is True

    def test_a_slot_never_names_an_instagram_FORMAT(self) -> None:
        # D17: format and visuals are chosen per post by performance and
        # relevance. Choosing one here would BE the rotation D17 forbids.
        slot = plan(slots=1, map_rows=DEEP, recent=[])["slots"][0]
        assert "format" not in slot and "visuals" not in slot


class TestEverySlotSaysWhat:
    def test_carries_the_stage_goal_the_slot_was_made_to_serve(self) -> None:
        result = plan(slots=3, map_rows=DEEP, recent=[])
        assert all(slot["goal"] for slot in result["slots"])
        assert result["slots"][0]["goal"] == "earn attention"

    def test_points_back_at_the_map_row_it_came_from(self) -> None:
        # `strategyRowId` on the subject row is how a post is traced to the idea
        # it came from, and `mark_strategy_row_used` needs the id to close it.
        assert plan(slots=1, map_rows=DEEP, recent=[])["slots"][0]["rowId"].startswith("row-")

    def test_numbers_the_slots_from_one(self) -> None:
        assert [s["slot"] for s in plan(slots=3, map_rows=DEEP, recent=[])["slots"]] == [1, 2, 3]
