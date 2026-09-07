"""The re-seed endpoint and the freshness view (S-A15).

The endpoint the portal calls when somebody saves a context document, so the
copy an agent reads stops drifting from the document a human edited. Before it,
all four populators were CLIs run by hand, and the workspace was a hand-seeded
copy that aged freely with nobody able to say by how much.

Two properties carry the weight here. The projection must be IDEMPOTENT, because
the portal will call it on every save and a rewrite-on-no-change would churn
`projectedAt` and destroy the one field the freshness view reads. And it must be
BEST EFFORT: the caller is a document-save handler, so a client with nothing to
project is an ordinary answer and never an exception, which would turn a failed
projection into a failed save.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.services.client_context import competitors_path, context_path
from tests.conftest import FakeWorkspaceStore
from tests.fake_firestore import FakeFirestoreClient

SLUG = "geektime"
CLIENT_ID = "client-1"


def seed_client(fake: FakeFirestoreClient, *, slug: str = SLUG) -> None:
    fake.documents[f"clients/{CLIENT_ID}"] = {"name": "Geektime", "agentsRepoSlug": slug}


def seed_doc(
    fake: FakeFirestoreClient,
    doc_type: str,
    *,
    content: str = "# Brand voice\n\nWarm, direct.",
    version: int = 7,
    tier: str = "internal",
    doc_id: str | None = None,
) -> None:
    fake.documents[f"clientContextDocs/{doc_id or ('ctx-' + doc_type)}"] = {
        "clientId": CLIENT_ID,
        "docType": doc_type,
        "tier": tier,
        "content": content,
        "version": version,
    }


def seed_competitor(fake: FakeFirestoreClient, company: str, **extra: Any) -> None:
    fake.documents[f"clientCompetitors/{company.lower()}"] = {
        "clientId": CLIENT_ID,
        "company": company,
        **extra,
    }


class TestReseed:
    def test_projects_a_document_with_full_provenance(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice")

        response = client.post(f"/clients/{SLUG}/context/reseed")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["written"] == 1
        outcome = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert outcome["outcome"] == "created"
        assert outcome["doc_version"] == 7

        stored = json.loads(fake_workspace.objects[context_path(SLUG, "brand-voice")])
        assert stored["markdown"].startswith("# Brand voice")
        source = stored["source"]
        assert source["docVersion"] == 7
        assert source["tier"] == "internal"
        assert source["projectedBy"] == "portal-save"
        assert source["contentHash"].startswith("sha256:")

    def test_a_second_save_with_the_same_text_writes_nothing(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        """The property that makes calling this on every save safe.

        Counting writes rather than comparing the stored value: an
        implementation that rewrote identical content would leave the same bytes
        behind and a state-only assertion would pass, while `projectedAt` moved
        on every save and the freshness view stopped meaning anything.
        """

        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice")

        first = client.post(f"/clients/{SLUG}/context/reseed").json()
        writes_after_first = len(fake_workspace.writes)
        second = client.post(f"/clients/{SLUG}/context/reseed").json()

        assert first["written"] == 1
        assert second["written"] == 0
        assert len(fake_workspace.writes) == writes_after_first
        outcome = next(d for d in second["documents"] if d["doc_type"] == "brand-voice")
        assert outcome["outcome"] == "unchanged"

    def test_a_bumped_version_rewrites_even_when_the_text_is_identical(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        # The C1 §4.3 amendment, end to end: skipping this would leave the
        # stored provenance claiming version 7 forever, and the freshness view
        # compares exactly those two numbers.
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", version=7)
        client.post(f"/clients/{SLUG}/context/reseed")

        seed_doc(fake_firestore_client, "brand-voice", version=8)
        second = client.post(f"/clients/{SLUG}/context/reseed").json()

        assert second["written"] == 1
        stored = json.loads(fake_workspace.objects[context_path(SLUG, "brand-voice")])
        assert stored["source"]["docVersion"] == 8

    def test_a_client_tier_document_is_never_projected(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        # The tier is named in the query, so this document is not merely
        # rejected -- it is never fetched. An agent grounded on the condensed
        # client-tier derivative would produce thinner work while looking fully
        # configured.
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", tier="client")

        body = client.post(f"/clients/{SLUG}/context/reseed").json()

        assert body["written"] == 0
        assert context_path(SLUG, "brand-voice") not in fake_workspace.objects

    def test_an_empty_document_is_skipped_with_a_reason(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", content="   \n ")

        body = client.post(f"/clients/{SLUG}/context/reseed").json()

        outcome = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert outcome["outcome"] == "skipped"
        assert "empty" in outcome["detail"]
        assert context_path(SLUG, "brand-voice") not in fake_workspace.objects

    def test_an_unknown_client_is_a_skip_not_an_error(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        """Best effort, per C1 invariant 4.5.

        The caller is a document-save handler. A 404 here would make the portal
        decide whether a failed projection should fail the save, and the
        contract already answers that: it must not.
        """

        response = client.post("/clients/nobody/context/reseed")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["written"] == 0
        assert body["documents"][0]["outcome"] == "skipped"
        assert "agentsRepoSlug" in body["documents"][0]["detail"]

    def test_competitors_are_projected_to_the_path_the_engine_reads(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        seed_client(fake_firestore_client)
        seed_competitor(fake_firestore_client, "Acme", url="https://acme.example")

        body = client.post(f"/clients/{SLUG}/context/reseed").json()

        assert body["competitors"]["outcome"] == "created"
        rows = json.loads(fake_workspace.objects[competitors_path(SLUG)])
        assert rows == [{"name": "Acme", "website": "https://acme.example"}]

    def test_no_competitors_writes_no_file_at_all(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        # `client.listCompetitors` reads a present-but-empty array as "we
        # looked, there are none" and a missing file as "never onboarded".
        # Writing [] would convert the honest answer into the wrong one.
        seed_client(fake_firestore_client)

        body = client.post(f"/clients/{SLUG}/context/reseed").json()

        assert body["competitors"]["outcome"] == "skipped"
        assert competitors_path(SLUG) not in fake_workspace.objects

    def test_projected_by_is_recorded_and_never_branched_on(
        self,
        client: TestClient,
        fake_firestore_client: FakeFirestoreClient,
        fake_workspace: FakeWorkspaceStore,
    ) -> None:
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "market-strategy")

        client.post(f"/clients/{SLUG}/context/reseed?projected_by=backfill")

        stored = json.loads(fake_workspace.objects[context_path(SLUG, "market-strategy")])
        assert stored["source"]["projectedBy"] == "backfill"


class TestFreshness:
    def test_a_projected_document_reads_fresh(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", version=3)
        client.post(f"/clients/{SLUG}/context/reseed")

        body = client.get(f"/clients/{SLUG}/context").json()

        assert body["is_current"] is True
        row = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert row["state"] == "fresh"
        assert row["projected_version"] == 3
        assert row["current_version"] == 3
        assert row["projected_at"]

    def test_a_portal_edit_after_projection_reads_stale(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        # The window C1 §5 exists to measure, and the whole reason the envelope
        # carries a version at all.
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", version=3)
        client.post(f"/clients/{SLUG}/context/reseed")
        seed_doc(fake_firestore_client, "brand-voice", content="rewritten", version=4)

        body = client.get(f"/clients/{SLUG}/context").json()

        row = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert row["state"] == "stale"
        assert (row["projected_version"], row["current_version"]) == (3, 4)
        assert body["is_current"] is False

    def test_a_document_never_projected_reads_absent(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice")

        body = client.get(f"/clients/{SLUG}/context").json()

        row = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert row["state"] == "absent"
        assert body["is_current"] is False

    def test_a_stale_copy_of_a_document_now_client_tier_only_reads_unprojectable(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        """Its own state, not folded into `absent`.

        A document that exists only at the `client` tier is not missing -- it is
        deliberately not projected. Reporting `absent` would send somebody
        looking for a write that is never coming.
        """

        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice", version=3)
        client.post(f"/clients/{SLUG}/context/reseed")
        seed_doc(fake_firestore_client, "brand-voice", version=4, tier="client")

        body = client.get(f"/clients/{SLUG}/context").json()

        row = next(d for d in body["documents"] if d["doc_type"] == "brand-voice")
        assert row["state"] == "unprojectable"
        # ...and it does not count against currency: nothing to close.
        assert body["is_current"] is True

    def test_documents_neither_side_holds_are_not_listed(
        self, client: TestClient, fake_firestore_client: FakeFirestoreClient
    ) -> None:
        # Most clients legitimately hold only some of the nine. Nine "absent"
        # rows per client would bury the ones that matter.
        seed_client(fake_firestore_client)
        seed_doc(fake_firestore_client, "brand-voice")

        body = client.get(f"/clients/{SLUG}/context").json()

        assert [d["doc_type"] for d in body["documents"]] == ["brand-voice"]


class TestUnconfigured:
    def test_the_routes_answer_503_naming_the_variable(
        self,
        settings: Any,
        database: Any,
        publisher_service: Any,
    ) -> None:
        """No bucket locally, and the rest of the service still works.

        Refusing to start without GCS would make everyone configure a bucket to
        work on the agent CRUD they were actually touching.
        """

        from contextlib import asynccontextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient as Client

        from app.main import build_services, create_app

        app = create_app()

        @asynccontextmanager
        async def lifespan(inner: FastAPI):  # type: ignore[no-untyped-def]
            build_services(inner, settings, database, publisher=publisher_service, workspace=None)
            yield

        app.router.lifespan_context = lifespan
        with Client(app) as unconfigured:
            response = unconfigured.post(f"/clients/{SLUG}/context/reseed")
            assert response.status_code == 503
            assert "GCS_ARTIFACTS_BUCKET" in response.json()["detail"]
            assert unconfigured.get(f"/clients/{SLUG}/context").status_code == 503
            # Unrelated routes are unaffected.
            assert unconfigured.get("/agents").status_code == 200
