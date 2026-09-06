"""Shared pytest fixtures.

Neither Google client is ever constructed for real: Pub/Sub is a fake publisher
that records what it published, and Firestore is the in-memory
:mod:`tests.fake_firestore`. The suite therefore needs no credentials, no
emulator and no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.firestore import FirestoreDB
from app.main import build_services, create_app
from app.services.publisher import PublisherService
from tests.fake_firestore import FakeFirestoreClient


class FakeWorkspaceStore:
    """In-memory stand-in for the agent-engine workspace bucket.

    Same arrangement as the Pub/Sub and Firestore fakes: the suite needs no
    credentials, no emulator and no network. ``writes`` counts calls rather than
    just recording the final state, because the property that matters for the
    projection is IDEMPOTENCE -- and a store that only remembered the last value
    could not tell a no-op from a rewrite with identical content.
    """

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.writes: list[str] = []

    def read_text(self, path: str) -> str | None:
        return self.objects.get(path)

    def write_text(self, path: str, body: str) -> None:
        self.objects[path] = body
        self.writes.append(path)


class FakePublisherClient:
    """Minimal stand-in for ``google.cloud.pubsub_v1.PublisherClient``."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self._next_id = 0

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attributes: str) -> Future:
        self._next_id += 1
        self.published.append((topic, data, attributes))
        future: Future = Future()
        future.set_result(str(self._next_id))
        return future


@pytest.fixture
def settings() -> Settings:
    """Settings for the suite: real services, fake backends, auth off.

    Auth is exercised directly in ``tests/test_security.py`` against its own
    app instances rather than by threading a token through all ~70 functional
    tests, which would test the fixture more than the behaviour.
    """

    return Settings(
        gcp_project_id="test-project",
        pubsub_job_topic_id="test-jobs-topic",
        auth_enabled=False,
    )


@pytest.fixture
def fake_publisher_client() -> FakePublisherClient:
    return FakePublisherClient()


@pytest.fixture
def fake_firestore_client() -> FakeFirestoreClient:
    return FakeFirestoreClient()


@pytest.fixture
def fake_workspace() -> FakeWorkspaceStore:
    return FakeWorkspaceStore()


@pytest.fixture
def publisher_service(
    settings: Settings, fake_publisher_client: FakePublisherClient
) -> PublisherService:
    return PublisherService(settings, client=fake_publisher_client)  # type: ignore[arg-type]


@pytest.fixture
def database(settings: Settings, fake_firestore_client: FakeFirestoreClient) -> FirestoreDB:
    return FirestoreDB(settings, client=fake_firestore_client)  # type: ignore[arg-type]


@pytest.fixture
def client(
    settings: Settings,
    database: FirestoreDB,
    publisher_service: PublisherService,
    fake_workspace: FakeWorkspaceStore,
) -> Iterator[TestClient]:
    """A TestClient whose services are wired to the in-memory backends.

    The real lifespan is replaced so startup never builds a Firestore or Pub/Sub
    client, but the services themselves are the production ones.
    """

    app = create_app()
    app.router.lifespan_context = _fake_lifespan(
        settings, database, publisher_service, fake_workspace
    )

    with TestClient(app) as test_client:
        yield test_client


def _fake_lifespan(
    settings: Settings,
    database: FirestoreDB,
    publisher: PublisherService,
    workspace: Any = None,
) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        build_services(app, settings, database, publisher=publisher, workspace=workspace)
        yield

    return lifespan


# --- Fixtures that seed a working agent -------------------------------------


@pytest.fixture
def agent(client: TestClient) -> dict[str, Any]:
    """A created agent with an active system prompt."""

    response = client.post(
        "/agents",
        json={
            "slug": "post-writer",
            "name": "Post Writer",
            "description": "Writes social posts",
            "agent_type": "post_writer",
            "model": "claude-opus-5",
            "model_params": {"temperature": 0.4},
            "tags": ["content"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()

    prompt = client.post(
        f"/agents/{created['id']}/prompts",
        json={"content": "You are a concise social post writer.", "notes": "first"},
    )
    assert prompt.status_code == 201, prompt.text
    return created


@pytest.fixture
def template(client: TestClient) -> dict[str, Any]:
    """A created template with an active version 1."""

    response = client.post(
        "/templates",
        json={
            "slug": "post-card",
            "name": "Post Card",
            "kind": "html_layout",
            "content": "<article>{{body}}</article>",
            "schema_definition": {"fields": ["body"]},
            "variables": ["body"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
