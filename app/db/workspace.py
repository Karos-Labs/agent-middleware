"""The agent-engine workspace bucket, as seen from the control plane.

agent-engine reads a client's grounding out of GCS, one JSON record per key::

    gs://<bucket>/clients/<slug>/client/profile.json
    gs://<bucket>/clients/<slug>/context/<docType>.json

Until now nothing in this service touched GCS at all -- templates store `gs://`
URIs and something else uploads the bytes -- so this is the first write path
from the API into the workspace. It is deliberately the smallest surface that
does the job: read one object as text, write one object as text, and nothing
else. No listing, no deletion, no signed URLs.

Two notes for whoever wires the deploy:

* The bucket is the EXISTING ``GCS_ARTIFACTS_BUCKET`` -- ``karoscmo-prep-agent-artifacts``
  and ``karoscmo-prod-agent-artifacts`` -- so no new variable is needed.
* The runtime service account needs ``roles/storage.objectAdmin`` on that
  bucket, in BOTH projects. A missing binding here fails at call time and not
  at deploy time: the revision comes up healthy and the projection 500s the
  first time the portal saves a document. That is the AU50 row that gets
  forgotten.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.config import Settings

logger = logging.getLogger(__name__)


class WorkspaceStore(Protocol):
    """The two operations the projection needs.

    A protocol rather than a base class so the test suite's in-memory store is
    not a subclass of anything -- the same arrangement ``FirestoreDB`` and the
    Pub/Sub publisher already have, which is why the suite needs no credentials,
    no emulator and no network.
    """

    def read_text(self, path: str) -> str | None:
        """Object contents, or ``None`` when it does not exist."""
        ...

    def write_text(self, path: str, body: str) -> None:
        """Create or replace the object at ``path``."""
        ...


class GcsWorkspaceStore:
    """A :class:`WorkspaceStore` backed by a real bucket."""

    def __init__(self, bucket_name: str, client: Any | None = None) -> None:
        self._bucket_name = bucket_name
        self._client = client
        self._bucket: Any | None = None

    def _resolve_bucket(self) -> Any:
        if self._bucket is None:
            if self._client is None:
                # Imported here, not at module scope: the whole test suite
                # imports this module and none of it should need the GCS client
                # resolved, let alone credentials.
                from google.cloud import storage  # type: ignore[attr-defined]

                self._client = storage.Client()
            self._bucket = self._client.bucket(self._bucket_name)
        return self._bucket

    def read_text(self, path: str) -> str | None:
        blob = self._resolve_bucket().blob(path)
        if not blob.exists():
            return None
        text: str = blob.download_as_text()
        return text

    def write_text(self, path: str, body: str) -> None:
        self._resolve_bucket().blob(path).upload_from_string(
            body, content_type="application/json"
        )


def build_workspace_store(settings: Settings) -> WorkspaceStore | None:
    """The store for this environment, or ``None`` when no bucket is configured.

    ``None`` rather than a broken store: locally there is no bucket, and a
    service that refused to start without one would make every developer
    configure GCS to run the agent CRUD they were actually working on. The
    routes that need it answer 503 with the variable named, which is a better
    failure than a stack trace from a client that could not authenticate.
    """

    if not settings.gcs_artifacts_bucket:
        logger.warning(
            "GCS_ARTIFACTS_BUCKET is not set: client-context projection is unavailable"
        )
        return None
    return GcsWorkspaceStore(settings.gcs_artifacts_bucket)
