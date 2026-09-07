"""Firestore access layer.

Everything the control plane persists lives in Firestore (Firebase, NoSQL).
This module is the only place that knows about the Firestore client itself; the
services above it work with collection/document references handed out here.

Layout
------
::

    agents/{agentId}                        agent document
    agents/{agentId}/prompts/{version}      immutable system prompt versions
    agents/{agentId}/examples/{exampleId}   few-shot examples
    agents/{agentId}/templates/{purpose}    template bindings (one per purpose)
    templates/{templateId}                  content template document
    templates/{templateId}/versions/{v}     immutable template versions
    agent_runs/{runId}                      dispatched jobs (root: run ids are global)
    run_feedback/{feedbackId}               reviewer verdicts (root: queried per agent)

Two deliberate choices make Firestore's lack of constraints tolerable:

* **Document ids carry meaning.** An agent's id *is* its slug, a prompt's id is
  its zero-padded version, a template binding's id is its purpose. Combined with
  ``DocumentReference.create()`` -- which fails if the document already exists --
  this gives real atomic uniqueness without a separate registry or transaction.
* **Runs and feedback are root collections.** They are queried per agent
  (``where("agent_id", "==", ...)``) rather than nested, so no collection-group
  indexes are needed and a run can be looked up by id alone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.async_collection import AsyncCollectionReference
from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.base_document import DocumentSnapshot

from app.config import Settings

logger = logging.getLogger(__name__)

# --- Collection names -------------------------------------------------------

AGENTS = "agents"
TEMPLATES = "templates"
RUNS = "agent_runs"
FEEDBACK = "run_feedback"
MODELS = "models"
MODEL_ACCESS_REQUESTS = "model_access_requests"
MODEL_ALIASES = "model_aliases"

# Subcollections.
PROMPTS = "prompts"
EXAMPLES = "examples"
AGENT_TEMPLATES = "templates"
TEMPLATE_VERSIONS = "versions"

VERSION_ID_WIDTH = 6
"""Version document ids are zero-padded so lexical id order matches numeric order."""


def utcnow() -> datetime:
    """Timezone-aware "now", used for every stored timestamp."""

    return datetime.now(UTC)


def generate_id() -> str:
    """Random document id for entities without a natural key."""

    return uuid.uuid4().hex


def version_doc_id(version: int) -> str:
    """Document id for version ``version`` of a prompt or template."""

    return f"{version:0{VERSION_ID_WIDTH}d}"


def snapshot_to_dict(snapshot: DocumentSnapshot) -> dict[str, Any]:
    """Flatten a snapshot into ``{"id": ..., **fields}``."""

    data = snapshot.to_dict() or {}
    return {**data, "id": snapshot.id}


class FirestoreDB:
    """Thin wrapper around the async Firestore client.

    Holds the collection-prefix convention (so several environments can share a
    Firestore database) and the client lifecycle.
    """

    def __init__(self, settings: Settings, client: AsyncClient | None = None) -> None:
        self._settings = settings
        self._prefix = settings.firestore_collection_prefix
        self._client = client if client is not None else self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> AsyncClient:
        if settings.firestore_emulator_host:
            # The client library reads this variable and skips credentials.
            import os

            os.environ.setdefault("FIRESTORE_EMULATOR_HOST", settings.firestore_emulator_host)
            logger.info("Using Firestore emulator at %s", settings.firestore_emulator_host)

        return AsyncClient(
            project=settings.resolved_firestore_project_id,
            database=settings.firestore_database,
        )

    @property
    def client(self) -> AsyncClient:
        return self._client

    # --- Reference helpers -------------------------------------------------

    def _path(self, parts: tuple[str, ...]) -> str:
        if not parts:
            raise ValueError("a Firestore path needs at least one segment")
        root, *rest = parts
        return "/".join([f"{self._prefix}{root}", *rest])

    def collection(self, *path: str) -> AsyncCollectionReference:
        """Collection reference for ``path``, e.g. ``("agents", slug, "prompts")``."""

        return self._client.collection(self._path(path))

    def document(self, *path: str) -> AsyncDocumentReference:
        """Document reference for ``path``, e.g. ``("agents", slug)``."""

        return self._client.document(self._path(path))

    # --- Lifecycle ---------------------------------------------------------

    async def ping(self) -> bool:
        """Round-trip a cheap read; used by the readiness probe."""

        try:
            await self.document("_health", "ping").get()
            return True
        except Exception:  # noqa: BLE001 - a probe must never raise
            logger.warning("Firestore ping failed", exc_info=True)
            return False

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
