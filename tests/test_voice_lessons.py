"""The counted half of B2 (SCRUM-494), as pure functions.

``client_preferences``' table comment has always said voice notes are derived
"from edits". The derivation never read one: it carried the notes a run wrote
about its own review cycle and the ``note`` actions a person typed, both of
which are somebody having already said the lesson in words. The edit pair --
the draft, and what the client published instead -- was stored, projected raw,
and never turned into a rule.

These cases are about the two ways that can go wrong. Deriving too little is a
column that stays empty. Deriving too much is worse: a lesson with one edit
behind it, stated to the next draft as though it were a rule, is how an agent
learns a habit from an accident.
"""

from __future__ import annotations

from app.services.voice_lessons import (
    MAX_LESSONS,
    derive_voice_notes,
    length_lesson,
    likes_from_posts,
    order_by_evidence,
    removed_words,
    skip_lessons,
    word_lessons,
    words,
)


class TestWhatCountsAsAWord:
    def test_drops_function_words_and_anything_short(self) -> None:
        assert words("We will leverage that synergy") == ["leverage", "synergy"]

    def test_reads_hebrew_by_the_same_rule_as_english(self) -> None:
        # One tokeniser, not a second one behind a language flag: the clients
        # write in both and half of them switch inside a sentence.
        assert "מינוף" in words("אנחנו נעשה מינוף של היתרון")

    def test_keeps_a_word_with_an_apostrophe_whole(self) -> None:
        assert words("the company's roadmap") == ["company's", "roadmap"]

    def test_ignores_bare_numbers(self) -> None:
        # "2026" removed from a draft is a date going stale, not a voice.
        assert words("shipped 2026 roadmap") == ["shipped", "roadmap"]


class TestWordsTheClientKeepsCutting:
    def test_two_edits_and_never_published_makes_a_lesson(self) -> None:
        pairs = [
            ("We leverage our platform", "We use our platform"),
            ("Leverage the data you have", "Use the data you have"),
        ]
        lessons = word_lessons(pairs)
        assert [lesson["lesson"] for lesson in lessons] == [
            'Takes "leverage" out: removed in 2 edits and never published once.'
        ]
        assert lessons[0]["evidence"] == 2

    def test_one_edit_is_an_edit_not_a_rule(self) -> None:
        assert word_lessons([("We leverage our platform", "We use our platform")]) == []

    def test_a_word_the_client_writes_back_somewhere_else_is_not_a_lesson(self) -> None:
        # THE CASE THIS RULE EXISTS FOR. A word can leave one draft because that
        # draft was about something else. Checking every FINAL text — not just
        # the ones it was cut from — is what separates "they do not want this
        # word" from "it did not belong in that post".
        pairs = [
            ("Our roadmap for growth", "Our plan for growth"),
            ("The roadmap is clear", "The plan is clear"),
            ("Here is the roadmap we promised", "Here is the roadmap we promised, in full"),
        ]
        assert word_lessons(pairs) == []

    def test_one_rant_cannot_manufacture_its_own_evidence(self) -> None:
        # The same word deleted six times inside ONE edit is one client making
        # one decision. Counting repetitions would let a single heavy rewrite
        # look like a pattern.
        pairs = [("synergy synergy synergy synergy synergy synergy", "value")]
        assert word_lessons(pairs) == []

    def test_carries_the_count_so_two_is_distinguishable_from_ten(self) -> None:
        pairs = [(f"draft {i} about leverage", f"draft {i}") for i in range(4)]
        assert word_lessons(pairs)[0]["evidence"] == 4


class TestLength:
    def test_states_the_median_direction_once_there_are_enough_edits(self) -> None:
        pairs = [("x" * 100, "x" * 50), ("y" * 200, "y" * 100), ("z" * 80, "z" * 40)]
        lesson = length_lesson(pairs)
        assert lesson is not None
        assert "shorter" in lesson["lesson"] and "50%" in lesson["lesson"]

    def test_two_edits_are_not_a_trend(self) -> None:
        assert length_lesson([("x" * 100, "x" * 10), ("y" * 100, "y" * 10)]) is None

    def test_a_small_median_shift_is_not_worth_saying(self) -> None:
        pairs = [("x" * 100, "x" * 97), ("y" * 100, "y" * 95), ("z" * 100, "z" * 99)]
        assert length_lesson(pairs) is None

    def test_one_wholesale_replacement_cannot_invent_a_trend(self) -> None:
        # The median rather than the mean, and this is the row that pays for it:
        # three ordinary edits and one draft the client threw away entirely.
        pairs = [
            ("x" * 100, "x" * 98),
            ("y" * 100, "y" * 99),
            ("z" * 100, "z" * 97),
            ("w" * 1000, "w" * 5),
        ]
        assert length_lesson(pairs) is None

    def test_notices_a_client_who_writes_longer(self) -> None:
        pairs = [("x" * 50, "x" * 100), ("y" * 50, "y" * 110), ("z" * 50, "z" * 90)]
        lesson = length_lesson(pairs)
        assert lesson is not None and "longer" in lesson["lesson"]


class TestSkips:
    def test_the_same_reason_twice_is_a_lesson(self) -> None:
        lessons = skip_lessons(["too salesy", "too salesy"])
        assert lessons[0]["lesson"] == 'Skipped 2 drafts for the same reason: "too salesy".'
        assert lessons[0]["source"] == "skips"

    def test_normalises_whitespace_and_case_before_grouping(self) -> None:
        lessons = skip_lessons(["Too  salesy", "too salesy ", " TOO SALESY"])
        assert len(lessons) == 1 and lessons[0]["evidence"] == 3

    def test_one_reason_given_once_is_not_a_rule(self) -> None:
        assert skip_lessons(["did not fit this week"]) == []


class TestLikes:
    def test_a_posted_row_becomes_a_like_that_names_the_post(self) -> None:
        likes = likes_from_posts([{"runId": "run-1", "subject": "the month-two cliff", "at": "2026-09-17T10:00:00Z"}])
        assert likes == [
            {"why": "posted as written", "subject": "the month-two cliff", "runId": "run-1", "at": "2026-09-17T10:00:00Z"}
        ]

    def test_says_why_on_every_row_so_nobody_reads_it_as_a_thumbs_up(self) -> None:
        # Nobody clicked a heart. A future reader of this column should not be
        # able to think they did.
        assert all(like["why"] == "posted as written" for like in likes_from_posts([{"runId": "r"}]))

    def test_survives_a_run_with_no_subject_row(self) -> None:
        # A pre-C7 run never wrote one, and the like is still true.
        assert likes_from_posts([{"runId": "run-9"}]) == [{"why": "posted as written", "runId": "run-9"}]

    def test_drops_a_row_that_names_nothing(self) -> None:
        assert likes_from_posts([{"at": "2026-09-17T10:00:00Z"}]) == []


class TestOrdering:
    def test_the_strongest_lesson_is_LAST(self) -> None:
        # Not a typo and not a preference: the engine reads these with
        # `.slice(-8)` (learning-context.ts), so the END of the list is what
        # reaches a draft. Sorting the obvious way would drop the best-evidenced
        # lesson the moment a client accumulates a ninth.
        ordered = order_by_evidence([{"lesson": "weak", "evidence": 1}, {"lesson": "strong", "evidence": 9}])
        assert [lesson["lesson"] for lesson in ordered] == ["weak", "strong"]

    def test_a_stated_lesson_with_no_count_sorts_as_one(self) -> None:
        ordered = order_by_evidence(
            [{"lesson": "counted", "evidence": 3}, {"lesson": "stated"}, {"lesson": "thin", "evidence": 1}]
        )
        assert ordered[-1]["lesson"] == "counted"


class TestTheWholeList:
    def test_keeps_what_somebody_stated_and_adds_what_is_counted(self) -> None:
        carried = [{"lesson": "never open with a question", "fromRunId": "run-1"}]
        notes = derive_voice_notes(
            carried=carried,
            edit_pairs=[("we leverage this", "we use this"), ("leverage that", "use that")],
            skip_reasons=[],
        )
        assert {n["lesson"] for n in notes} == {
            "never open with a question",
            'Takes "leverage" out: removed in 2 edits and never published once.',
        }

    def test_a_stated_lesson_wins_a_tie_with_a_derived_one(self) -> None:
        carried = [{"lesson": 'Takes "leverage" out: removed in 2 edits and never published once.'}]
        notes = derive_voice_notes(
            carried=carried,
            edit_pairs=[("we leverage this", "we use this"), ("leverage that", "use that")],
            skip_reasons=[],
        )
        assert len(notes) == 1

    def test_caps_the_list_by_dropping_the_WEAKEST(self) -> None:
        # The cap and the ordering have to agree, and this is the case that
        # catches them disagreeing: `[:MAX]` on a weakest-first list would throw
        # away the best-evidenced lesson and keep the noise.
        carried = [{"lesson": f"stated {i}"} for i in range(MAX_LESSONS + 5)]
        notes = derive_voice_notes(
            carried=carried,
            edit_pairs=[(f"draft {i} about leverage", f"draft {i}") for i in range(6)],
            skip_reasons=[],
        )
        assert len(notes) == MAX_LESSONS
        # Both counted lessons have six edits behind them and both survive; which
        # of the two lands last is the sort being stable and does not matter. What
        # matters is that neither was dropped for a "stated" lesson with one.
        assert [note.get("evidence") for note in notes[-2:]] == [6, 6]
        assert all(note.get("evidence", 1) == 1 for note in notes[:-2])

    def test_no_feedback_at_all_derives_nothing_rather_than_failing(self) -> None:
        assert derive_voice_notes(carried=[], edit_pairs=[], skip_reasons=[]) == []
