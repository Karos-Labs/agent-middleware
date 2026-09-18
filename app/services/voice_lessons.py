"""Voice lessons derived from what the client actually did to a draft (B2).

``client_preferences``' own table comment has said this since the migration
landed: *"``voice_notes`` and ``likes`` are derived -- voice notes only from
edits and review revisions (Craft 11 §3)"*. The derivation never read an edit.
It took the ``voiceNotes`` a run wrote about its own review cycle, and ``note``
actions somebody typed — both of which are a person or a model already having
said the lesson in words. The one signal in the log that nobody had to phrase,
and the one the portal calls *"the single most useful signal the loop has"*, is
the pair of texts on a ``posted_with_edits`` row: the draft, and what the client
published instead. Those were stored, projected raw into the next prompt, and
never turned into a rule.

WHY THIS IS ARITHMETIC AND NOT A MODEL CALL. A lesson from a diff could be
written by a model, and it would read better. It would also cost a model call on
every feedback event — on a path that is best-effort bookkeeping, inside a
database transaction, for a client who may click four times in a row — and it
would produce a sentence nobody can check against the data it came from. The raw
pair ALREADY reaches the next draft through the ``feedback`` projection, so the
derived lesson does not need to be prose. It needs to be the thing the pair
cannot say on its own: **what the client does again and again**. One edit is an
edit. The same word taken out of four drafts is a voice.

So everything here is counted, not inferred:

* a word or phrase removed in several separate edits and never written back
* a consistent direction of length change across several edits
* the same reason given for skipping several drafts

Each lesson carries its own evidence count, so a reader (and the next person to
change this) can see exactly how thin or thick the ground under it is.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any

#: A word has to be at least this long to count as content. Hebrew and English
#: both put their function words under it (``and``, ``the``, ``של``, ``את``),
#: which is most of the filtering this needs and none of the language detection.
MIN_WORD_CHARS = 4

#: The rest of the function words, for the two languages the clients write in.
#: Deliberately short: a stopword list is a place defects hide, and the evidence
#: threshold below is what actually does the work. A word that survives this list
#: but is noise still has to be removed from several drafts and never written
#: back before it becomes a lesson.
STOPWORDS: frozenset[str] = frozenset(
    {
        # English
        "that", "this", "with", "from", "have", "will", "your", "their", "there",
        "then", "than", "when", "what", "which", "into", "about", "just", "like",
        "they", "them", "been", "were", "would", "could", "should", "over",
        "only", "also", "some", "more", "most", "very", "much", "here", "here's",
        "because", "after", "before", "while", "these", "those", "does", "doing",
        # Hebrew
        "שלנו", "שלהם", "שלכם", "אנחנו", "אתם", "הזה", "הזאת", "האלה", "כאשר",
        "אבל", "יותר", "בגלל", "בתוך", "מאוד", "עדיין", "ולכן", "כלומר",
    }
)

#: Words are whatever a run of letters, digits or apostrophes is — Unicode aware,
#: so Hebrew is tokenised by the same rule as English rather than by a second one.
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

#: How many separate edits a word has to be removed from before it is a lesson.
#: Two is the floor at which "again" is true at all; the count travels with the
#: lesson so a reader can tell two from ten.
MIN_EDITS_FOR_WORD = 2

#: The same, for a reason given when skipping a draft.
MIN_SKIPS_FOR_REASON = 2

#: How many edits before a length trend is worth stating, and how big the median
#: change has to be. Three edits and 15% keep "the client trims a bit" out of the
#: prompt while catching a client who halves everything.
MIN_EDITS_FOR_LENGTH = 3
MIN_LENGTH_SHIFT = 0.15

#: Never let this fill the whole prompt. The consumer takes the LAST eight (see
#: the ordering note in ``order_by_evidence``), so this cap is about the stored
#: row rather than about what a run reads.
MAX_LESSONS = 24


def words(text: str) -> list[str]:
    """The content words of a piece of copy, lowercased, in order."""

    return [
        w
        for w in (m.group(0).lower() for m in _WORD.finditer(text or ""))
        if len(w) >= MIN_WORD_CHARS and w not in STOPWORDS and not w.isdigit()
    ]


def removed_words(original: str, final: str) -> set[str]:
    """Content words the client took OUT of one draft.

    A set rather than a count: a word deleted five times inside one post is still
    one client, one decision. What makes a lesson is the same word going in
    several DIFFERENT edits, and counting repetitions inside a single edit would
    let one ranty rewrite manufacture the evidence for it.
    """

    return set(words(original)) - set(words(final))


def word_lessons(pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Words the client keeps removing and never writes back.

    The second half of that sentence is what keeps this honest. A word can leave
    one draft because that draft was about something else; a word the client has
    removed from three drafts and never once published is a word they do not
    want. Checking every FINAL text — not just the ones it was removed from —
    is what tells those apart.
    """

    if not pairs:
        return []

    published: set[str] = set()
    for _, final in pairs:
        published.update(words(final))

    removals: Counter[str] = Counter()
    for original, final in pairs:
        for word in removed_words(original, final):
            if word not in published:
                removals[word] += 1

    lessons: list[dict[str, Any]] = []
    for word, count in removals.most_common():
        if count < MIN_EDITS_FOR_WORD:
            continue
        lessons.append(
            {
                "lesson": f'Takes "{word}" out: removed in {count} edits and never published once.',
                "evidence": count,
                "source": "edits",
            }
        )
    return lessons


def length_lesson(pairs: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Whether the client consistently cuts or pads, stated as the median.

    The MEDIAN rather than the mean, because one draft the client replaced
    wholesale is exactly the outlier that would otherwise invent a trend out of
    four ordinary edits.
    """

    usable = [(o, f) for o, f in pairs if len(o) > 0]
    if len(usable) < MIN_EDITS_FOR_LENGTH:
        return None

    shifts = [(len(f) - len(o)) / len(o) for o, f in usable]
    median = statistics.median(shifts)
    if abs(median) < MIN_LENGTH_SHIFT:
        return None

    direction = "shorter" if median < 0 else "longer"
    return {
        "lesson": (
            f"Rewrites {direction}: the published version is a median "
            f"{abs(median) * 100:.0f}% {direction} across {len(usable)} edits."
        ),
        "evidence": len(usable),
        "source": "edits",
    }


def skip_lessons(reasons: list[str]) -> list[dict[str, Any]]:
    """The same reason given for dropping several drafts.

    NOT written into ``never_topics``. That column is a person's, by the table's
    own design, and a client who skipped three drafts for being "too salesy" has
    not asked us to ban a topic — they have told us something about tone. It goes
    where tone goes.
    """

    counted: Counter[str] = Counter(
        " ".join(r.split()).strip().lower() for r in reasons if r and r.strip()
    )
    lessons: list[dict[str, Any]] = []
    for reason, count in counted.most_common():
        if count < MIN_SKIPS_FOR_REASON:
            continue
        lessons.append(
            {
                "lesson": f'Skipped {count} drafts for the same reason: "{reason}".',
                "evidence": count,
                "source": "skips",
            }
        )
    return lessons


def likes_from_posts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the client published without changing a word.

    ``likes`` has been a column with no writer since the migration, so the
    ticket's *"likes with the post they referred to"* had nowhere to come from.
    This is the strongest endorsement the data actually contains: a ``posted``
    action, as opposed to ``posted_with_edits``, is the client putting our
    sentences out under their own name.

    ``why`` is on every row on purpose. Nobody clicked a heart, and a future
    reader of this table should not be able to mistake this for one.
    """

    likes: list[dict[str, Any]] = []
    for row in rows:
        subject = row.get("subject")
        run_id = row.get("runId") or row.get("run_id")
        if not subject and not run_id:
            continue
        like: dict[str, Any] = {"why": "posted as written"}
        if subject:
            like["subject"] = subject
        if run_id:
            like["runId"] = run_id
        if row.get("at"):
            like["at"] = row["at"]
        likes.append(like)
    return likes


def order_by_evidence(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Weakest first, strongest LAST, and that is not a typo.

    The engine reads these with ``.slice(-8)``
    (``packages/workflow/src/primitives/learning-context.ts``), so the END of the
    list is what reaches a draft. Sorting strongest-first — the obvious way —
    would put the best-evidenced lesson in the prompt only while the list is
    short, and quietly drop it the moment a client accumulates a ninth.

    Lessons with no ``evidence`` (the run-written and hand-typed ones this
    derivation has always carried) sort as 1: they are a single statement each,
    which is what they are.
    """

    return sorted(lessons, key=lambda lesson: int(lesson.get("evidence", 1) or 1))


def derive_voice_notes(
    *,
    carried: list[dict[str, Any]],
    edit_pairs: list[tuple[str, str]],
    skip_reasons: list[str],
) -> list[dict[str, Any]]:
    """The whole derived list, in the order the consumer reads it.

    ``carried`` is what the existing derivation already produces — the review
    notes a run recorded and the ``note`` actions a person typed. They are kept
    exactly as they were: this adds to the evidence, it does not replace a
    lesson somebody stated outright with one counted from a diff.
    """

    derived: list[dict[str, Any]] = []
    derived.extend(word_lessons(edit_pairs))
    length = length_lesson(edit_pairs)
    if length is not None:
        derived.append(length)
    derived.extend(skip_lessons(skip_reasons))

    # De-duplicated on the sentence, so a re-derivation cannot double a lesson
    # and the carried list wins when it says the same thing.
    seen = {str(lesson.get("lesson", "")).strip().lower() for lesson in carried}
    fresh = [lesson for lesson in derived if str(lesson["lesson"]).strip().lower() not in seen]

    # The LAST ``MAX_LESSONS``, not the first. With the weakest-first ordering
    # above, ``[:MAX_LESSONS]`` would keep the twenty-four thinnest lessons and
    # throw away the best-evidenced one — the exact inversion this ordering
    # exists to avoid, one line after setting it up.
    return order_by_evidence(carried + fresh)[-MAX_LESSONS:]


__all__ = [
    "MAX_LESSONS",
    "MIN_EDITS_FOR_LENGTH",
    "MIN_EDITS_FOR_WORD",
    "MIN_SKIPS_FOR_REASON",
    "derive_voice_notes",
    "length_lesson",
    "likes_from_posts",
    "order_by_evidence",
    "removed_words",
    "skip_lessons",
    "word_lessons",
    "words",
]
