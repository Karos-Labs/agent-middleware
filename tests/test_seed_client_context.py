"""The client-context projection script, and specifically its topic catalog.

The pure builders are what is worth testing here: the script's I/O half is two
GCS blob writes guarded by an ``exists()`` check, while the ``--skeleton``
payloads are where a mistake reaches an agent as either a blocked run or, worse,
invented client data.

THE PROPERTY THAT ACTUALLY MATTERS is that the seeded topic catalog can serve a
reservation. ``topics.reserve`` in agent-engine refuses any reservation that
would leave a lane below ``LANE_FLOOR`` (5), so a catalog of five rows is
indistinguishable in effect from no catalog at all — and no catalog at all is
the state prep was actually in: nothing in either repo writes that file, so
every ``topics.reserve`` call breached the floor forever, which killed every
instagram-agent run at step 03.
"""

from __future__ import annotations

import json
from typing import Any

from scripts.seed_client_context import (
    PROJECTED_DOC_TYPES,
    TOPIC_DEFAULT_LANE,
    TOPIC_LANE_FLOOR,
    TOPIC_SUBJECT_TEMPLATES,
    build_competitors,
    build_context_record,
    build_profile,
    context_record_is_current,
    skeleton_extras,
    skeleton_topics_catalog,
)

CLIENT: dict[str, Any] = {
    "name": "Geektime",
    "agentsRepoSlug": "geektime",
    "industry": "B2B SaaS",
    "brandingGuidelines": {"primaryAccent": "#123456"},
}


class TestTopicCatalog:
    def test_seeds_more_rows_than_the_lane_floor_so_a_reservation_can_succeed(self) -> None:
        rows = skeleton_topics_catalog(CLIENT, "geektime")
        # Strictly greater, not >=: `topics.reserve` compares
        # `available - count < LANE_FLOOR`, so exactly FLOOR rows still breaches.
        assert len(rows) > TOPIC_LANE_FLOOR
        # Two weeks of daily cadence on top of the floor is what
        # carousel-agent-v2's SKILL.md step 04 asks a seeded lane to hold.
        assert len(rows) - TOPIC_LANE_FLOOR >= 14

    def test_every_row_matches_the_engine_TopicRecord_shape(self) -> None:
        for row in skeleton_topics_catalog(CLIENT, "geektime"):
            # The four fields `karos-topics` actually reads. `reservationKey` is
            # written by the tool itself on reserve, never seeded.
            assert set(row) == {"_placeholder", "topic", "normalized", "status", "lane"}
            assert row["status"] == "available"
            assert row["lane"] == TOPIC_DEFAULT_LANE
            assert row["topic"].strip() == row["topic"]
            # `normalizeTopic` in the engine is `trim().toLowerCase()`.
            assert row["normalized"] == row["topic"].strip().lower()

    def test_rows_are_distinct_after_substitution(self) -> None:
        # `topics.reserve` dedups on `normalized`, so two templates collapsing
        # onto one string would seed a row that can never be reserved separately.
        rows = skeleton_topics_catalog(CLIENT, "geektime")
        assert len({r["normalized"] for r in rows}) == len(rows)

    def test_lands_in_the_lane_instagram_agent_actually_asks_for(self) -> None:
        # instagram-agent is the only caller that passes a lane, and it defaults
        # to DEFAULT_CAROUSEL_LANE. Seeding any other lane leaves it breaching.
        assert TOPIC_DEFAULT_LANE == "general"
        assert {r["lane"] for r in skeleton_topics_catalog(CLIENT, "geektime")} == {"general"}

    def test_marks_every_row_as_a_placeholder(self) -> None:
        # The catalog is a JSON array with no envelope to hang one marker on, so
        # the marker rides on each row. Anyone opening the file must be able to
        # tell nobody chose these.
        assert all(r["_placeholder"] is True for r in skeleton_topics_catalog(CLIENT, "geektime"))

    def test_subjects_are_prompts_rather_than_claims(self) -> None:
        # The line that makes this seedable at all: a subject to write about
        # asserts nothing, so the drafting agent still has to research and source
        # it. A seeded statistic or milestone would be fabricated client data.
        for template in TOPIC_SUBJECT_TEMPLATES:
            assert "%" not in template
            assert not any(ch.isdigit() for ch in template)

    def test_degrades_to_a_neutral_subject_when_the_client_declares_no_industry(self) -> None:
        rows = skeleton_topics_catalog({}, "someclient")
        assert len(rows) > TOPIC_LANE_FLOOR
        assert all("{industry}" not in r["topic"] for r in rows)

    def test_uses_the_client_s_own_industry_when_there_is_one(self) -> None:
        rows = skeleton_topics_catalog(CLIENT, "geektime")
        assert any("B2B SaaS" in r["topic"] for r in rows)


class TestSkeletonExtras:
    def test_writes_the_catalog_to_the_path_the_engine_reads(self) -> None:
        # `CATALOG_SEGMENTS = ["topics", "catalog"]` under the store's
        # `clients/<slug>/` prefix, plus `.json`.
        path, payload = skeleton_extras(CLIENT, "geektime")["topics/catalog"]
        assert path == "clients/geektime/topics/catalog.json"
        assert isinstance(payload, list)

    def test_still_carries_the_three_per_agent_extras(self) -> None:
        # The catalog is additive; nothing it replaced should have gone away.
        assert set(skeleton_extras(CLIENT, "geektime")) == {
            "landing/brand",
            "landing/intake",
            "memory/beliefs",
            "topics/catalog",
        }


class TestProfileProjection:
    def test_industry_reaches_the_profile_the_fallback_reads(self) -> None:
        # instagram-agent's new step-03 fallback derives its subject from
        # `client/profile.json`'s `industry`, so a client whose portal record has
        # one must end up with that key here — otherwise the fallback's last rung
        # is unreachable and the run holds.
        assert build_profile(CLIENT)["industry"] == "B2B SaaS"

    def test_an_absent_industry_stays_absent_rather_than_becoming_a_guess(self) -> None:
        assert "industry" not in build_profile({"name": "Nameless", "agentsRepoSlug": "n"})


# --- Context document projection (C1) ---------------------------------------
#
# docs/contracts/C1-client-context.md. These test the builders, in the same
# spirit as the topic catalog above: the I/O half is blob writes behind an
# exists() check, and everything that can go wrong in a way an agent would feel
# is a decision made in these functions.


PROJECTED_AT = "2026-08-27T09:00:00Z"


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "clientId": "client-1",
        "docType": "brand-voice",
        "tier": "internal",
        "content": "# Brand voice\n\nWarm, direct, never breathless.",
        "version": 7,
    }
    base.update(overrides)
    return base


def _record(**overrides: Any) -> dict[str, Any] | None:
    return build_context_record(
        _doc(**overrides),
        doc_type=overrides.get("docType", "brand-voice"),
        firestore_doc_id="ctx-abc",
        projected_at=PROJECTED_AT,
        projected_by="seed-cli",
    )


class TestContextRecord:
    def test_carries_the_full_provenance_the_freshness_report_reads(self) -> None:
        record = _record()
        assert record is not None
        assert record["docType"] == "brand-voice"
        assert record["markdown"].startswith("# Brand voice")

        source = record["source"]
        # Every one of these is read by something: docVersion by the freshness
        # comparison, contentHash by the idempotency check, tier by anyone
        # asking which half of a two-tier document reached the model.
        assert source["firestoreDocId"] == "ctx-abc"
        assert source["docVersion"] == 7
        assert source["tier"] == "internal"
        assert source["projectedAt"] == PROJECTED_AT
        assert source["projectedBy"] == "seed-cli"
        assert source["contentHash"].startswith("sha256:")

    def test_the_field_is_markdown_not_content(self) -> None:
        # Firestore calls it `content`; the workspace envelope calls it
        # `markdown`, matching StrategyDocument. Renaming it in one place is
        # the point -- the alternative is a third prose shape in the workspace.
        record = _record()
        assert record is not None
        assert "markdown" in record and "content" not in record

    def test_a_client_tier_document_is_never_projected(self) -> None:
        # The one that would be invisible in production: the client tier is a
        # condensed ~50% derivative, so an agent grounded on it produces
        # thinner work while looking fully configured.
        assert _record(tier="client") is None

    def test_an_internal_only_document_is_never_projected(self) -> None:
        # client-guidelines and action-plan live here. They are out of v1
        # because publishing them to an agent is a product decision.
        assert _record(tier="internal-only", docType="action-plan") is None

    def test_an_empty_document_is_refused_like_a_missing_one(self) -> None:
        # client.getStrategy's own reason, which this adopts verbatim: "an
        # empty document is worse than a missing one: it would silently hand
        # the model no charter while looking configured".
        assert _record(content="") is None
        assert _record(content="   \n\t  ") is None
        assert _record(content=None) is None

    def test_an_unknown_version_projects_as_permanently_stale(self) -> None:
        # Fails toward loud rather than toward lost: the content is real and
        # worth having, and 0 sorts below every real version, so the readiness
        # report shows it stale until the next portal write bumps it.
        record = _record(version=None)
        assert record is not None
        assert record["source"]["docVersion"] == 0

    def test_every_projected_doc_type_is_a_real_portal_doc_type(self) -> None:
        # Guards against a typo becoming a document that is never found. These
        # nine are the ContextDocType union in karosCMO minus meeting-notes
        # (noisy) and the two internal-only ones.
        assert len(PROJECTED_DOC_TYPES) == 9
        assert set(PROJECTED_DOC_TYPES) == {
            "brand-voice",
            "market-strategy",
            "competitor-analysis",
            "product-information",
            "branding-guidelines",
            "target-audience",
            "x-agent-profile",
            "linkedin-agent-profile",
            "reddit-agent-profile",
        }


class TestContextIdempotency:
    def test_identical_content_is_a_no_op_even_in_a_later_run(self) -> None:
        # THE test for this half. The hash covers the markdown alone, so a
        # second pass matches despite a different projectedAt. Hashing the
        # serialized record -- which is what the client/* records do -- could
        # never match once the envelope carries a timestamp, every run would
        # count as an update, and projectedAt would churn on every pass,
        # destroying the field the freshness report reads.
        first = _record()
        assert first is not None
        stored = json.dumps(first)

        later = build_context_record(
            _doc(),
            doc_type="brand-voice",
            firestore_doc_id="ctx-abc",
            projected_at="2026-12-25T18:30:00Z",
            projected_by="backfill",
        )
        assert later is not None
        assert context_record_is_current(stored, later)

    def test_changed_content_is_not_current(self) -> None:
        first = _record()
        assert first is not None
        changed = _record(content="# Brand voice\n\nCold and clipped.")
        assert changed is not None
        assert not context_record_is_current(json.dumps(first), changed)

    def test_a_bumped_version_alone_still_rewrites(self) -> None:
        # Found by writing this test, and it changed the implementation.
        # ClientContextDoc.version is bumped on EVERY portal write and a write
        # need not change the text. Treating identical markdown at version 8 as
        # current would leave the stored provenance claiming 7 forever, and the
        # freshness report compares exactly those two numbers -- so that
        # document would read stale for the rest of its life. A permanent false
        # stale costs more than a redundant one-kilobyte write, because the
        # report is only worth reading if a "stale" line means something.
        first = _record(version=7)
        assert first is not None
        bumped = _record(version=8)
        assert bumped is not None
        assert not context_record_is_current(json.dumps(first), bumped)

    def test_identical_content_at_the_same_version_is_still_a_no_op(self) -> None:
        # ...and the tightening above must not have cost the property it
        # protects: an unchanged document projected twice writes nothing.
        first = _record(version=7)
        assert first is not None
        assert context_record_is_current(json.dumps(first), _record(version=7))

    def test_a_missing_or_unreadable_target_is_not_current(self) -> None:
        candidate = _record()
        assert candidate is not None
        assert not context_record_is_current(None, candidate)
        assert not context_record_is_current("", candidate)
        assert not context_record_is_current("{ this is not json", candidate)
        assert not context_record_is_current("[]", candidate)
        assert not context_record_is_current('{"markdown": "no source block"}', candidate)


class TestCompetitors:
    def test_maps_the_portal_columns_onto_the_tool_interface(self) -> None:
        rows = [
            {
                "id": "row-1",
                "clientId": "client-1",
                "company": "Acme Capital",
                "url": "https://acme.example",
                "marketTier": "Leader",
                "overlap": "High",
                "keyStrengths": ["distribution"],
                "source": "report",
            }
        ]

        out = build_competitors(rows)

        assert out == [
            {
                "name": "Acme Capital",
                "website": "https://acme.example",
                "marketTier": "Leader",
                "overlap": "High",
                "keyStrengths": ["distribution"],
            }
        ]
        # Portal bookkeeping does not reach a prompt: everything in this record
        # is passed to the model verbatim by client.listCompetitors.
        assert "id" not in out[0] and "clientId" not in out[0] and "source" not in out[0]

    def test_a_row_with_no_company_is_dropped(self) -> None:
        # The name is the identity. A nameless competitor in a prompt is an
        # invitation to write about nobody.
        assert build_competitors([{"url": "https://x.example"}]) == []
        assert build_competitors([{"company": "   "}]) == []

    def test_absent_fields_stay_absent(self) -> None:
        out = build_competitors([{"company": "Solo"}])
        assert out == [{"name": "Solo"}]
