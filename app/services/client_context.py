"""Projecting a client's documents into the agent-engine workspace (C1).

``docs/contracts/C1-client-context.md``. The agent reads a projected COPY of
the client's documents and never Firestore, so the projection carries enough
provenance for the readiness report to measure how stale that copy has become.

This module holds the whole of it, and both callers use it: the CLI seeder
(``scripts/seed_client_context.py``) and the endpoint the portal calls when a
document is saved. That is the point of it living here rather than in the
script. Two implementations of the same envelope would drift, and the drift
would be silent -- an agent grounded on a record written by whichever path ran
last, with no way to tell which.

The Firestore collections read here -- ``clients``, ``clientContextDocs``,
``clientCompetitors`` -- belong to karosCMO, not to this service. They are read
through ``db.client`` directly rather than ``db.collection()``, because the
latter applies ``FIRESTORE_COLLECTION_PREFIX``, and a prefix is this service's
own namespacing convention. Applying it to somebody else's collection would
look for ``dev_clients`` and find nothing, reporting a client with no documents
rather than an error.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from app.db.firestore import FirestoreDB
from app.db.workspace import WorkspaceStore

logger = logging.getLogger(__name__)

#: karosCMO's own collections. Not prefixed -- see the module docstring.
CLIENTS = "clients"
CLIENT_CONTEXT_DOCS = "clientContextDocs"
CLIENT_COMPETITORS = "clientCompetitors"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so an absent field stays absent rather than becoming ''."""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


# --- Context documents (C1) -------------------------------------------------
#
# See docs/contracts/C1-client-context.md. The agent reads a PROJECTED COPY of
# the client's documents and never Firestore, so the projection carries enough
# provenance for the readiness report to measure how stale that copy is.

#: The nine docTypes projected in v1. `meeting-notes` is excluded as noisy;
#: `client-guidelines` and `action-plan` are the `internal-only` tier and need
#: a product decision before an agent ever sees them.
PROJECTED_DOC_TYPES: tuple[str, ...] = (
    "brand-voice",
    "market-strategy",
    "competitor-analysis",
    "product-information",
    "branding-guidelines",
    "target-audience",
    # Complement `strategy/<agent>` rather than replacing it: that one is the
    # charter (what the account is FOR), these are the identity narrative.
    "x-agent-profile",
    "linkedin-agent-profile",
    "reddit-agent-profile",
)

#: The only tier projected, and never with a fallback.
#:
#: `clientContextDocs` is keyed by (clientId, docType, tier), not by docType --
#: `getClientContextDocByTier` in karosCMO exists because a client-facing
#: document and its internal twin share a docType and an unordered `.limit(1)`
#: used to return whichever Firestore felt like. The `client` tier is a
#: condensed ~50% derivative, so falling back to it would ground an agent on
#: half a document WHILE LOOKING FULLY CONFIGURED. A docType present only at
#: another tier is absent, and reported as absent.
PROJECTED_TIER = "internal"


def build_context_record(
    doc: dict[str, Any],
    *,
    doc_type: str,
    firestore_doc_id: str,
    projected_at: str,
    projected_by: str,
) -> dict[str, Any] | None:
    """The C1 envelope for one context document, or ``None`` to skip it.

    Returns ``None`` rather than raising, because a client missing one document
    is an ordinary state and the caller reports it alongside every other gap.

    Two refusals, both deliberate:

    * A document at any tier other than ``internal``. See ``PROJECTED_TIER``.
    * Empty or whitespace-only content. ``client.getStrategy`` already returns
      ``not_available`` for both a missing file and an empty one, for the
      reason written in its own source -- "an empty document is worse than a
      missing one: it would silently hand the model no charter while looking
      configured". Writing the empty one would put that exact object on disk.
    """

    if doc.get("tier") != PROJECTED_TIER:
        return None

    markdown = doc.get("content")
    if not isinstance(markdown, str) or not markdown.strip():
        return None

    raw_version = doc.get("version")
    # An unknown version sorts below every real one, so the document reads as
    # STALE in the readiness report until the next portal write bumps it. That
    # is the right direction to fail: the content is real and worth having, and
    # a permanently-stale row is loud, where refusing to project would throw
    # away good grounding over a bookkeeping gap.
    version = raw_version if isinstance(raw_version, int) else 0

    return {
        "docType": doc_type,
        # `markdown`, not `content`: this matches StrategyDocument, which is the
        # envelope agent-engine already reads prose from. The rename happens
        # once, here, rather than adding a third shape to the workspace.
        "markdown": markdown,
        "source": {
            "firestoreDocId": firestore_doc_id,
            "docVersion": version,
            "tier": PROJECTED_TIER,
            "projectedAt": projected_at,
            "projectedBy": projected_by,
            "contentHash": f"sha256:{_sha(markdown)}",
        },
    }


def context_record_is_current(existing: str | None, candidate: dict[str, Any]) -> bool:
    """Whether the stored record already asserts exactly what this one would.

    Two things have to match, and the second is not in the C1 draft.

    ``source.contentHash`` -- a hash of the MARKDOWN ALONE, not of the
    serialized record the way the ``client/*`` records above are compared. That
    difference is load-bearing: this envelope carries ``projectedAt``, so a
    whole-body comparison could never match, every run would count as an
    update, and ``projectedAt`` would churn on every pass -- destroying the one
    field the freshness report reads.

    ``source.docVersion`` as well, because ``ClientContextDoc.version`` is
    bumped on EVERY portal write and a write does not have to change the text.
    Identical markdown at version 8 over a projection recorded at version 7 is
    not a no-op: skipping it leaves the stored provenance claiming 7 forever,
    and the freshness report -- which compares exactly those two numbers --
    reports that document stale for the rest of its life. A permanent false
    stale is worse than a redundant one-kilobyte write, because the report's
    whole value is that a "stale" line means something.
    """

    if not existing:
        return False
    try:
        stored = json.loads(existing)
    except ValueError:
        # Unreadable JSON on the target is not "current"; overwrite it.
        return False
    if not isinstance(stored, dict):
        return False
    source = stored.get("source")
    if not isinstance(source, dict):
        return False
    stored_hash = source.get("contentHash")
    if not stored_hash or stored_hash != candidate["source"]["contentHash"]:
        return False
    return source.get("docVersion") == candidate["source"]["docVersion"]


#: Fields carried from a `clientCompetitors` row into the workspace list.
#:
#: `client.listCompetitors` types a competitor as `{name, website?, ...}` with
#: everything else passed through to the model verbatim, so this is a curated
#: set rather than the whole row: `id`, `clientId` and `source` are portal
#: bookkeeping that would reach a prompt as noise.
_COMPETITOR_FIELDS: tuple[str, ...] = (
    "marketTier",
    "overlap",
    "positioning",
    "scale",
    "keyStrengths",
    "keyWeaknesses",
    "threatLevel",
    "founded",
    # How often the AI answer engines named this brand in the last visibility
    # capture. Absent means never measured, which is why it is not defaulted.
    "llmMentions",
)


def build_competitors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `client/competitors.json` array agent-engine already reads.

    Nothing in either repository writes that path today, so
    ``client.listCompetitors`` returns ``not_available`` for every client in
    every environment.

    ``company`` -> ``name`` and ``url`` -> ``website`` is the whole mapping: the
    portal names the column after a company and the tool's interface after a
    competitor. A row with no company is dropped rather than given a blank name
    -- the name IS the identity here, and a nameless competitor in a prompt is
    an invitation to write about nobody.
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        company = row.get("company")
        if not isinstance(company, str) or not company.strip():
            continue
        record = {"name": company.strip(), "website": row.get("url")}
        record.update({key: row.get(key) for key in _COMPETITOR_FIELDS})
        out.append(_clean(record))
    return out


# --- The projection ---------------------------------------------------------


@dataclass
class DocumentOutcome:
    """What happened to one projected record."""

    doc_type: str
    outcome: str  # created | updated | unchanged | skipped
    detail: str = ""
    doc_version: int | None = None


@dataclass
class ProjectionResult:
    """What happened to one client."""

    slug: str
    documents: list[DocumentOutcome] = field(default_factory=list)
    competitors: DocumentOutcome | None = None

    @property
    def written(self) -> int:
        return sum(1 for d in self.documents if d.outcome in ("created", "updated"))


@dataclass
class ProjectedDocument:
    """The freshness view of one record already in the workspace."""

    doc_type: str
    projected_version: int | None
    current_version: int | None
    projected_at: str | None

    @property
    def state(self) -> str:
        """``fresh`` | ``stale`` | ``absent`` | ``unprojectable``.

        ``unprojectable`` is its own answer rather than folded into ``absent``:
        a document that exists in Firestore only at the ``client`` tier is not
        missing, it is deliberately not projected, and a reader chasing an
        ``absent`` line would go looking for a write that will never happen.
        """

        if self.current_version is None:
            return "unprojectable"
        if self.projected_version is None:
            return "absent"
        return "fresh" if self.projected_version >= self.current_version else "stale"


class ClientContextProjector:
    """Projects one client's documents into the workspace bucket."""

    def __init__(self, db: FirestoreDB, store: WorkspaceStore) -> None:
        self._db = db
        self._store = store

    # --- Firestore reads (karosCMO's collections) -------------------------

    async def _client_id(self, slug: str) -> str | None:
        """The clients document id for an ``agentsRepoSlug``.

        Queried rather than assumed: the workspace is keyed by
        ``agentsRepoSlug`` and Firestore by document id, and they are not the
        same value.
        """

        query = (
            self._db.client.collection(CLIENTS)
            .where(filter=FieldFilter("agentsRepoSlug", "==", slug))
            .limit(1)
        )
        async for snapshot in query.stream():
            return str(snapshot.id)
        return None

    async def _context_docs(self, client_id: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """The internal-tier context documents for one client, by docType.

        The tier is named IN THE QUERY, not filtered afterwards. A post-filter
        is one refactor away from becoming a fallback, and a fallback to the
        ``client`` tier would ground an agent on a condensed ~50% derivative
        while looking fully configured.
        """

        query = (
            self._db.client.collection(CLIENT_CONTEXT_DOCS)
            .where(filter=FieldFilter("clientId", "==", client_id))
            .where(filter=FieldFilter("tier", "==", PROJECTED_TIER))
        )
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        async for snapshot in query.stream():
            row = snapshot.to_dict() or {}
            doc_type = row.get("docType")
            if doc_type in PROJECTED_DOC_TYPES:
                found[str(doc_type)] = (str(snapshot.id), row)
        return found

    async def _competitor_rows(self, client_id: str) -> list[dict[str, Any]]:
        query = self._db.client.collection(CLIENT_COMPETITORS).where(
            filter=FieldFilter("clientId", "==", client_id)
        )
        return [snapshot.to_dict() or {} async for snapshot in query.stream()]

    # --- Writes -----------------------------------------------------------

    async def project(self, slug: str, *, projected_by: str = "portal-save") -> ProjectionResult:
        """Project one client. Never raises for missing data.

        A client with no documents is an ordinary state and comes back as a
        result full of ``skipped``, because the caller is a document-save
        handler in the portal: C1 invariant 4.5 makes projection best-effort
        from that side, and an exception here would turn a failed projection
        into a failed save.
        """

        result = ProjectionResult(slug=slug)
        client_id = await self._client_id(slug)
        if client_id is None:
            result.documents.append(
                DocumentOutcome("*", "skipped", f"no client with agentsRepoSlug={slug!r}")
            )
            return result

        # One timestamp for the whole pass: two documents projected in the same
        # call were projected at the same moment, and a per-write `utcnow()`
        # would make them differ by milliseconds for no reason anyone can use.
        projected_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        docs = await self._context_docs(client_id)

        for doc_type in PROJECTED_DOC_TYPES:
            found = docs.get(doc_type)
            if found is None:
                continue
            firestore_doc_id, row = found
            record = build_context_record(
                row,
                doc_type=doc_type,
                firestore_doc_id=firestore_doc_id,
                projected_at=projected_at,
                projected_by=projected_by,
            )
            if record is None:
                result.documents.append(
                    DocumentOutcome(doc_type, "skipped", "empty content")
                )
                continue
            result.documents.append(self._write_context(slug, doc_type, record))

        result.competitors = self._write_competitors(slug, await self._competitor_rows(client_id))
        logger.info(
            "Projected client %s: %d document(s) written of %d",
            slug,
            result.written,
            len(result.documents),
        )
        return result

    def _write_context(
        self, slug: str, doc_type: str, record: dict[str, Any]
    ) -> DocumentOutcome:
        path = context_path(slug, doc_type)
        existing = self._store.read_text(path)
        version = int(record["source"]["docVersion"])
        if context_record_is_current(existing, record):
            # A no-op, which means projectedAt is left ALONE. Rewriting an
            # identical record with a fresh timestamp would make every save
            # look like a change to anything reading that field.
            return DocumentOutcome(doc_type, "unchanged", doc_version=version)
        self._store.write_text(path, _serialise(record))
        return DocumentOutcome(
            doc_type, "updated" if existing else "created", doc_version=version
        )

    def _write_competitors(self, slug: str, rows: list[dict[str, Any]]) -> DocumentOutcome:
        competitors = build_competitors(rows)
        if not competitors:
            # An empty list is NOT written. `client.listCompetitors` treats a
            # present-but-empty array as a normal success with no competitors,
            # and a missing file as "never onboarded" -- so writing [] would
            # convert an honest "not set up" into "we looked, there are none".
            return DocumentOutcome("competitors", "skipped", "no rows in the portal")
        path = competitors_path(slug)
        body = _serialise(competitors)
        existing = self._store.read_text(path)
        if existing is not None and _sha(existing) == _sha(body):
            return DocumentOutcome("competitors", "unchanged")
        self._store.write_text(path, body)
        return DocumentOutcome(
            "competitors", "updated" if existing else "created", f"{len(competitors)} rows"
        )

    # --- Freshness --------------------------------------------------------

    async def freshness(self, slug: str) -> list[ProjectedDocument]:
        """Projected version against current version, per document.

        The signature the readiness report checks, and the reason the envelope
        carries provenance at all. Served from here rather than computed by the
        report because the report reaches GCS with a human's credentials, while
        the portal has this endpoint and no bucket access.
        """

        client_id = await self._client_id(slug)
        if client_id is None:
            return []
        docs = await self._context_docs(client_id)
        out: list[ProjectedDocument] = []
        for doc_type in PROJECTED_DOC_TYPES:
            found = docs.get(doc_type)
            current = None
            if found is not None:
                raw = found[1].get("version")
                current = raw if isinstance(raw, int) else 0
            stored = self._store.read_text(context_path(slug, doc_type))
            projected_version, projected_at = _read_provenance(stored)
            if current is None and projected_version is None:
                # Neither side has it. Not a gap worth a line: most clients
                # legitimately hold only some of the nine, and nine "absent"
                # rows per client would bury the ones that matter.
                continue
            out.append(
                ProjectedDocument(
                    doc_type=doc_type,
                    projected_version=projected_version,
                    current_version=current,
                    projected_at=projected_at,
                )
            )
        return out


def context_path(slug: str, doc_type: str) -> str:
    return f"clients/{slug}/context/{doc_type}.json"


def competitors_path(slug: str) -> str:
    return f"clients/{slug}/client/competitors.json"


def _serialise(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _read_provenance(stored: str | None) -> tuple[int | None, str | None]:
    """``(docVersion, projectedAt)`` from a stored record, tolerantly.

    A record this cannot parse counts as not projected rather than as an error:
    the freshness view exists to be read at a glance, and one unreadable object
    should show as a gap to fix, not break the whole client's report.
    """

    if not stored:
        return None, None
    try:
        parsed = json.loads(stored)
    except ValueError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    source = parsed.get("source")
    if not isinstance(source, dict):
        return None, None
    version = source.get("docVersion")
    projected_at = source.get("projectedAt")
    return (
        version if isinstance(version, int) else None,
        projected_at if isinstance(projected_at, str) else None,
    )
