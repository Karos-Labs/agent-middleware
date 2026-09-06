"""The C4 capability descriptor: coverage, vocabulary, and what is derived.

docs/contracts/C4-capability-descriptor.md. The router selects an agent on
`capabilities x platforms x consumesMedia` and never on the words in its name,
so these check the two things that would quietly break that: a product with no
descriptor (the router falls back to nothing and the agent is unreachable), and
a descriptor whose derived halves disagree with the code they were derived from.

The identity set is agent-engine's KNOWN_PRODUCT_IDS: a slug is a real agent
when the engine can dispatch it. Everything else -- the catalog, the readiness
report, the portal's key map -- reconciles to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.enums import AgentStatus, Capability
from scripts.report_client_readiness import AGENT_REQUIREMENTS
from scripts.seed_all_agents import (
    CATALOG,
    DESCRIPTORS,
    LEGACY_ONLY_AGENTS,
    build_document,
    build_legacy_document,
    descriptor_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: agent-engine's KNOWN_PRODUCT_IDS, verbatim at 89bb8c4. Copied rather than
#: read across the repository boundary: the seam is the wire, not a relative
#: path into a sibling checkout that may not exist on a CI runner. The test
#: below fails if the catalog drifts from it, which is the drift that matters.
KNOWN_PRODUCT_IDS = (
    "x-agent",
    "instagram-agent",
    "linkedin-agent",
    "reddit-agent",
    "blog-agent",
    "newsletter-agent",
    "campaign-orchestrator",
    "landing-builder-agent",
    "branded-shorts-agent",
    "reputation-agent",
    "seo-geo-agent",
    "intel-report-agent",
    "tiktok-agent",
)


@pytest.fixture(scope="module")
def stages() -> dict[str, list[dict[str, Any]]]:
    return json.loads((REPO_ROOT / "scripts" / "engine_stages.json").read_text(encoding="utf-8"))


class TestCoverage:
    def test_every_engine_product_has_a_catalog_row(self) -> None:
        # campaign-orchestrator is why this test exists: it was dispatchable by
        # the engine with no row here at all, so it could not have a descriptor
        # and the router could not see it.
        assert {e["slug"] for e in CATALOG} == set(KNOWN_PRODUCT_IDS)

    def test_every_engine_product_has_a_descriptor(self) -> None:
        assert set(DESCRIPTORS) == set(KNOWN_PRODUCT_IDS)

    def test_the_readiness_report_scores_only_dispatchable_products(self) -> None:
        """No phantom rows, in either direction.

        Both setup agents used to be scored here with NO requirements, so every
        client counted two free `ready` products -- agents that could not fail
        because they could not run, inflating both halves of "N/M client-agent
        pairs can run today".

        campaign-orchestrator is the allowed absence: it has no per-client
        workspace requirements of its own, and inventing some to make the sets
        match would be worse than the gap.
        """

        scored = set(AGENT_REQUIREMENTS)
        assert scored <= set(KNOWN_PRODUCT_IDS)
        assert set(KNOWN_PRODUCT_IDS) - scored == {"campaign-orchestrator"}

    def test_the_engine_stage_map_covers_the_same_products(self, stages: dict) -> None:
        assert set(stages) == set(KNOWN_PRODUCT_IDS)


class TestVocabulary:
    def test_every_declared_capability_is_in_the_closed_vocabulary(self) -> None:
        allowed = {c.value for c in Capability}
        for slug, row in DESCRIPTORS.items():
            unknown = set(row.get("capabilities", [])) - allowed
            assert not unknown, f"{slug} declares {unknown}, which is not in Capability"

    def test_every_product_declares_at_least_one_capability(self) -> None:
        # A descriptor with no capability is unroutable, which for a working
        # engine product is a silent hole rather than a statement.
        for slug, row in DESCRIPTORS.items():
            assert row.get("capabilities"), f"{slug} declares no capability"

    def test_the_vocabulary_has_no_unused_verbs(self) -> None:
        """A verb nothing claims is either a missing descriptor or dead vocabulary.

        `run_setup` is the one worth watching: both setup agents were retired,
        and it survives only because linkedin-agent and reddit-agent absorbed
        the intake as their `00-channel-setup` pre-flight. If this fails on
        run_setup, that absorption was undone and the capability should go.
        """

        claimed = {c for row in DESCRIPTORS.values() for c in row.get("capabilities", [])}
        unused = {c.value for c in Capability} - claimed
        assert not unused, f"vocabulary entries nothing claims: {sorted(unused)}"


class TestDerivedHalves:
    def test_gates_come_from_the_stages_and_every_product_has_one(
        self, stages: dict
    ) -> None:
        """All thirteen pause for a human somewhere.

        Worth asserting as a set rather than per agent: `gates` was empty for
        every product until the generator learned the three ways the engine
        declares a gate, and an empty list is exactly what a planner reads as
        "this finishes on its own". Four of the thirteen use a kind other than
        batch_review, which is why grepping for batch_review undercounted.
        """

        for slug in KNOWN_PRODUCT_IDS:
            descriptor = descriptor_for(slug, stages[slug])
            assert descriptor["gates"], f"{slug} reports no human gate"

    def test_seo_geo_reports_both_of_its_gates(self, stages: dict) -> None:
        assert descriptor_for("seo-geo-agent", stages["seo-geo-agent"])["gates"] == [
            "prompt_set_review",
            "fix_generation_review",
        ]

    def test_readiness_is_read_from_the_report_not_retyped(self, stages: dict) -> None:
        descriptor = descriptor_for("x-agent", stages["x-agent"])
        assert "client/config:xHandle" in descriptor["readiness"]["hard"]
        assert descriptor["readiness"]["soft"] == ["topics/catalog"]
        # Hard and soft are disjoint: a path that is hard for one requirement
        # and soft for another is hard, or an agent would advertise a blocking
        # gap as merely degrading.
        assert not set(descriptor["readiness"]["hard"]) & set(descriptor["readiness"]["soft"])

    def test_a_product_the_report_does_not_score_gets_empty_readiness(
        self, stages: dict
    ) -> None:
        readiness = descriptor_for("campaign-orchestrator", stages["campaign-orchestrator"])[
            "readiness"
        ]
        assert readiness == {"hard": [], "soft": []}

    def test_target_date_is_false_everywhere_because_nothing_reads_it(
        self, stages: dict
    ) -> None:
        """`targetDate` appears nowhere in agent-engine.

        C3's first principle is that no displayed field goes unread. Advertising
        support here would put that exact failure on the descriptor, and a
        planner would promise a client a publish date the run never sees. Each
        row flips as its agent learns to read it (T-A13).
        """

        for slug in KNOWN_PRODUCT_IDS:
            assert descriptor_for(slug, stages[slug])["supports_target_date"] is False

    def test_only_the_three_agents_that_read_media_declare_it(self, stages: dict) -> None:
        # Verified by grep across agents/*/src, not inferred from the product's
        # name. landing-builder-agent is the tempting false positive: C3 routes
        # its `references` into mediaAssets, and that work has not landed.
        consuming = {
            slug
            for slug in KNOWN_PRODUCT_IDS
            if descriptor_for(slug, stages[slug])["consumes_media"]
        }
        assert consuming == {"instagram-agent", "tiktok-agent", "branded-shorts-agent"}


class TestPortalKeyMapping:
    def test_no_portal_key_routes_to_two_products(self) -> None:
        seen: dict[str, str] = {}
        for slug, row in DESCRIPTORS.items():
            for key in row.get("custom_agent_keys", []):
                assert key not in seen, f"{key} routes to both {seen[key]} and {slug}"
                seen[key] = slug

    def test_the_two_channels_whose_setup_was_inlined_carry_both_keys(self) -> None:
        # The reason customAgentKeys is a list. A single-valued field cannot
        # express this, and the consistency test C4 asks for could never pass.
        assert set(DESCRIPTORS["linkedin-agent"]["custom_agent_keys"]) == {
            "karos-linkedin-writer-v2",
            "karos-linkedin-setup-v2",
        }
        assert set(DESCRIPTORS["reddit-agent"]["custom_agent_keys"]) == {
            "karos-reddit-runner",
            "karos-reddit-setup",
        }

    def test_legacy_agents_are_present_and_unroutable(self) -> None:
        assert len(LEGACY_ONLY_AGENTS) == 5
        for entry in LEGACY_ONLY_AGENTS:
            document = build_legacy_document(entry, "NOW")
            assert document["status"] == AgentStatus.LEGACY_ONLY.value
            # Present and empty, not absent: the router gets a definite
            # "nothing here" rather than a missing field to interpret.
            assert document["capabilities"] == []
            assert document["custom_agent_keys"] == [entry["slug"]]
            assert document["is_public"] is False

    def test_a_legacy_slug_never_collides_with_an_engine_product(self) -> None:
        assert not {e["slug"] for e in LEGACY_ONLY_AGENTS} & set(KNOWN_PRODUCT_IDS)


class TestSeededDocument:
    def test_the_descriptor_fields_reach_the_document(self, stages: dict) -> None:
        entry = next(e for e in CATALOG if e["slug"] == "x-agent")
        document = build_document(entry, stages["x-agent"], "NOW")
        assert document["capabilities"] == ["draft_social_post"]
        assert document["platforms"] == ["x"]
        assert document["gates"] == ["batch_review"]
        assert document["readiness"]["hard"]

    def test_campaign_orchestrator_is_seeded_but_not_client_facing(self, stages: dict) -> None:
        entry = next(e for e in CATALOG if e["slug"] == "campaign-orchestrator")
        document = build_document(entry, stages["campaign-orchestrator"], "NOW")
        assert document["is_public"] is False
        assert document["capabilities"] == ["orchestrate_campaign"]

    def test_every_other_product_stays_public(self, stages: dict) -> None:
        for entry in CATALOG:
            if entry["slug"] == "campaign-orchestrator":
                continue
            document = build_document(entry, stages[entry["slug"]], "NOW")
            assert document["is_public"] is True, entry["slug"]
