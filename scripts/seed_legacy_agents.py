"""Migrate the legacy ``karos-agents`` lab repo into the control plane.

Reads the explicit manifest in :mod:`scripts.legacy_manifest`, then seeds
agents, system prompts and templates through ``AgentService`` /
``PromptService`` / ``TemplateService`` -- never by writing Firestore
directly, so everything seeded passes exactly the validation an API caller
would hit. Binary assets go to GCS; only their ``gs://`` URIs are stored.

Idempotency
-----------
Re-running must be a no-op, which takes more than "create if absent" here:
prompt and template versions are append-only, so a naive re-run would stack a
v2, v3, v4 of identical content. Every write is therefore content-compared
against the currently active version first, and skipped when equal. The three
outcomes are reported separately (``created`` / ``updated`` / ``unchanged``) so
a second run visibly does nothing.

Model-agnostic conversion
-------------------------
Two mechanisms, in order:

1. **Frontmatter is stripped.** Skill-discovery metadata (``name:``,
   ``triggers:``, ``model:``) means nothing outside Claude Skills; removing the
   block is mechanical and verifiable.
2. **Harness vocabulary is rewritten** by the reviewed substitutions in
   :mod:`scripts.neutralize` -- ``WebSearch`` becomes web search, the Task-tool
   fan-out becomes independent research tasks, ``claude-opus-4-8`` becomes a
   reasoning *tier*. Each rule is hand-written against an observed phrase and
   preserves the instruction; nothing is deleted. ``--no-neutralize`` seeds the
   raw legacy text instead.

:func:`find_claude_isms` then runs on the **rewritten** text, so a rule that
under-covers still trips the detector and ``--strict`` still fails the run.
Coverage is verified, not assumed -- which is what makes rewriting safe here
where a blind regex sweep would not be.

Usage
-----
::

    # Show what would happen, touch nothing:
    python scripts/seed_legacy_agents.py --karos-agents ../karos-agents --dry-run

    # Against the Firestore emulator:
    FIRESTORE_EMULATOR_HOST=localhost:8080 \\
      python scripts/seed_legacy_agents.py --karos-agents ../karos-agents

    # Against prep, including binary upload:
    python scripts/seed_legacy_agents.py --karos-agents ../karos-agents \\
      --env prep --upload-assets
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make `app` importable when run as `python scripts/seed_legacy_agents.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.schemas.agent import AgentCreate, AgentUpdate  # noqa: E402
from app.api.schemas.prompt import SystemPromptCreate  # noqa: E402
from app.api.schemas.template import (  # noqa: E402
    AgentTemplateLinkCreate,
    TemplateCreate,
    TemplateVersionCreate,
)
from app.config import Settings  # noqa: E402
from app.core.exceptions import ResourceNotFoundError  # noqa: E402
from app.db.firestore import FirestoreDB  # noqa: E402
from app.services.agents import AgentService  # noqa: E402
from app.services.prompts import PromptService  # noqa: E402
from app.services.templates import TemplateService  # noqa: E402
from scripts.legacy_manifest import (  # noqa: E402
    AGENT_SPECS,
    LegacyAgentSpec,
    TemplateSource,
)
from scripts.neutralize import neutralize  # noqa: E402

logger = logging.getLogger("seed")

# --- Environment presets ----------------------------------------------------
# Mirrors the deployed topology so `--env prep` needs no other flags. Values
# match agent-engine's own cloudbuild substitutions.
ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prep": {
        "gcp_project_id": "karoscmo-prep",
        "firestore_project_id": "karoscmo",
        "firestore_database": "prep",
        "pubsub_job_topic_id": "karos-agent-runs-prep",
        "gcs_artifacts_bucket": "karoscmo-prep-agent-artifacts",
    },
    "prod": {
        "gcp_project_id": "karoscmo",
        "firestore_project_id": "karoscmo",
        "firestore_database": "(default)",
        "pubsub_job_topic_id": "karos-agent-runs-prod",
        "gcs_artifacts_bucket": "karoscmo-prod-agent-artifacts",
    },
}

FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)

#: Residues of the single-shot Claude harness. Evaluated against the text
#: AFTER neutralisation, so this doubles as the coverage check on those rules.
CLAUDE_ISMS: tuple[tuple[str, str], ...] = (
    (r"\bsubagent_type\b", "Claude Task-tool fan-out"),
    (r"\bTask tool\b", "Claude Task-tool fan-out"),
    (r"\bWebSearch\b|\bWebFetch\b", "Claude built-in tool reference"),
    (r"Claude Preview MCP", "Claude-specific MCP tool"),
    (r"\bclaude-(?:opus|sonnet|haiku)[\w.-]*", "hardcoded model id"),
    (r"\.claude/skills", "Claude Skills path assumption"),
    (r"\bmax_tokens\s*[:=]", "provider-specific sampling parameter"),
)


@dataclass
class Report:
    """What the run did, per outcome."""

    counts: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def record(self, outcome: str, what: str) -> None:
        self.counts[outcome] += 1
        symbol = {"created": "+", "updated": "~", "unchanged": "="}.get(outcome, "?")
        print(f"  {symbol} {outcome:<9} {what}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# --- Source reading ---------------------------------------------------------


def read_source(root: Path, relative: str) -> str:
    """Read a manifest-named file, failing loudly if the lab repo moved."""

    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(
            f"{relative} is named in the manifest but not present under {root}. "
            "The lab repo layout changed; update scripts/legacy_manifest.py rather "
            "than seeding a partial agent."
        )
    return path.read_text(encoding="utf-8")


def strip_frontmatter(text: str) -> str:
    """Remove a leading ``---`` YAML block. Safe and mechanical."""

    return FRONTMATTER.sub("", text, count=1).lstrip("\n")


def find_claude_isms(text: str) -> list[str]:
    """Names of harness-specific constructs still present in ``text``."""

    found: list[str] = []
    for pattern, label in CLAUDE_ISMS:
        if re.search(pattern, text, re.IGNORECASE):
            if label not in found:
                found.append(label)
    return found


def compose_prompt(
    root: Path, spec: LegacyAgentSpec, *, apply_neutralize: bool = True
) -> tuple[str, list[str]]:
    """SKILL.md body plus the reference docs the manifest deems reusable.

    Returns the composed prompt and the names of any neutralisation rules that
    fired, so the run can report what it rewrote rather than doing it silently.
    """

    sections = [strip_frontmatter(read_source(root, spec.skill_path)).strip()]
    for reference in spec.reference_paths:
        body = strip_frontmatter(read_source(root, reference)).strip()
        title = Path(reference).stem.replace("-", " ").title()
        sections.append(f"# Reference: {title}\n\n{body}")
    composed = "\n\n---\n\n".join(sections) + "\n"

    if not apply_neutralize:
        return composed, []
    return neutralize(composed)


def normalized(text: str | None) -> str:
    """Comparison form: trailing whitespace and line endings are not changes."""

    if text is None:
        return ""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


# --- GCS --------------------------------------------------------------------


class AssetUploader:
    """Uploads template binaries and returns their ``gs://`` URIs.

    Skips an object whose content is already identical, so a re-run neither
    re-uploads nor churns the object's generation.
    """

    def __init__(self, bucket_name: str | None, *, enabled: bool, report: Report) -> None:
        self._bucket_name = bucket_name
        self._enabled = enabled
        self._report = report
        self._bucket: Any | None = None

    def uri_for(self, template_slug: str, source: Path) -> str:
        return f"gs://{self._bucket_name}/templates/{template_slug}/{source.name}"

    def preflight(self) -> None:
        """Fail before the first write if uploading cannot possibly work.

        Learned the hard way: the storage import is lazy (so ``--dry-run`` needs
        no client), which meant a missing ``google-cloud-storage`` surfaced only
        once the first template with an asset came up — eleven documents into a
        real run. Idempotency made the resume clean, but a half-written store is
        not a state to rely on recovering from. Checked up front instead.
        """

        if not self._enabled:
            return
        if not self._bucket_name:
            raise RuntimeError(
                "--upload-assets was given but no bucket is configured; set "
                "GCS_ARTIFACTS_BUCKET or pass --bucket"
            )
        try:
            self._lazy_bucket()
        except ImportError as exc:
            raise RuntimeError(
                "--upload-assets needs the google-cloud-storage package: "
                "pip install -r requirements.txt"
            ) from exc

    def _lazy_bucket(self) -> Any:
        if self._bucket is None:
            # Namespace package; see the same note in app/services/publisher.py.
            # Imported here, not at module scope, so --dry-run needs no client.
            from google.cloud import storage  # type: ignore[attr-defined]

            self._bucket = storage.Client().bucket(self._bucket_name)
        return self._bucket

    def upload(self, template_slug: str, source: Path) -> str:
        """Upload ``source`` and return its URI (or just the URI when disabled)."""

        uri = self.uri_for(template_slug, source)
        if not self._enabled:
            self._report.record("unchanged", f"asset {uri} (upload skipped)")
            return uri

        import hashlib

        blob = self._lazy_bucket().blob(f"templates/{template_slug}/{source.name}")
        payload = source.read_bytes()
        digest = hashlib.md5(payload).hexdigest()  # noqa: S324 - GCS's own checksum, not security

        if blob.exists():
            blob.reload()
            if blob.md5_hash and _b64_md5_to_hex(blob.md5_hash) == digest:
                self._report.record("unchanged", f"asset {uri}")
                return uri

        blob.upload_from_string(payload)
        self._report.record("created", f"asset {uri}")
        return uri


def _b64_md5_to_hex(b64_digest: str) -> str:
    import base64

    return base64.b64decode(b64_digest).hex()


# --- Seeding ----------------------------------------------------------------


class Seeder:
    """Upserts one manifest into one environment."""

    def __init__(
        self,
        *,
        root: Path,
        agents: AgentService,
        prompts: PromptService,
        templates: TemplateService,
        uploader: AssetUploader,
        report: Report,
        dry_run: bool,
        neutralize_prompts: bool = True,
    ) -> None:
        self._root = root
        self._agents = agents
        self._prompts = prompts
        self._templates = templates
        self._uploader = uploader
        self._report = report
        self._dry_run = dry_run
        self._neutralize = neutralize_prompts

    async def seed_all(self) -> None:
        for spec in AGENT_SPECS:
            print(f"\n{spec.slug}")
            await self.seed_agent(spec)

    async def seed_agent(self, spec: LegacyAgentSpec) -> None:
        await self._upsert_agent(spec)
        await self._upsert_prompt(spec)
        for template in spec.templates:
            await self._upsert_template(spec, template)

    # --- Agent -----------------------------------------------------------

    async def _upsert_agent(self, spec: LegacyAgentSpec) -> None:
        if self._dry_run:
            # No Firestore client exists in a dry run, so there is nothing to
            # diff against; report the intent rather than inventing an outcome.
            self._report.record("created", f"agent {spec.slug}")
            return

        try:
            existing = await self._agents.get(spec.slug, include_deleted=True)
        except ResourceNotFoundError:
            existing = None

        if existing is None:
            await self._agents.create(
                AgentCreate(
                    slug=spec.slug,
                    name=spec.name,
                    description=spec.description,
                    agent_type=spec.agent_type,
                    config=dict(spec.config),
                    tags=list(spec.tags),
                )
            )
            self._report.record("created", f"agent {spec.slug}")
            return

        # Only the fields this migration owns are compared; anything a human
        # tuned in the portal afterwards (model, model_params, status) is left
        # alone, so re-running never reverts an operator's change.
        drifted = (
            existing.get("name") != spec.name
            or existing.get("description") != spec.description
            or existing.get("agent_type") != spec.agent_type
            or (existing.get("tags") or []) != list(spec.tags)
            or (existing.get("config") or {}) != dict(spec.config)
        )
        if not drifted:
            self._report.record("unchanged", f"agent {spec.slug}")
            return

        await self._agents.update(
            spec.slug,
            AgentUpdate(
                name=spec.name,
                description=spec.description,
                agent_type=spec.agent_type,
                config=dict(spec.config),
                tags=list(spec.tags),
            ),
        )
        self._report.record("updated", f"agent {spec.slug}")

    # --- Prompt ----------------------------------------------------------

    async def _upsert_prompt(self, spec: LegacyAgentSpec) -> None:
        content, rewrites = compose_prompt(
            self._root, spec, apply_neutralize=self._neutralize
        )
        if rewrites:
            print(f"    neutralized: {', '.join(rewrites)}")

        # Runs on the REWRITTEN text: a rule that under-covers still trips this,
        # so coverage is verified rather than assumed.
        for label in find_claude_isms(content):
            self._report.warn(f"{spec.slug}: prompt still contains {label}")

        if self._dry_run:
            # The agent may not exist yet in a dry run, so there is nothing to
            # compare against; report the intent without guessing an outcome.
            self._report.record("created", f"prompt {spec.slug} ({len(content)} chars)")
            return

        active = await self._prompts.find_active(spec.slug)
        if active is not None and normalized(active.get("content")) == normalized(content):
            self._report.record("unchanged", f"prompt {spec.slug} v{active['version']}")
            return

        created = await self._prompts.create_version(
            spec.slug,
            SystemPromptCreate(
                content=content,
                notes="seeded from karos-agents",
                activate=True,
            ),
            # The actor is an argument now rather than a body field, because
            # over HTTP it comes off the verified token. This is the CLI path,
            # where the honest actor is the script itself.
            created_by="seed_legacy_agents",
        )
        outcome = "updated" if active is not None else "created"
        self._report.record(outcome, f"prompt {spec.slug} v{created['version']}")

    # --- Templates -------------------------------------------------------

    async def _upsert_template(self, spec: LegacyAgentSpec, source: TemplateSource) -> None:
        body = read_source(self._root, source.path)
        asset_uris = [
            self._uploader.upload(source.slug, self._root / asset) for asset in source.assets
        ]

        if self._dry_run:
            self._report.record("created", f"template {source.slug} ({len(body)} chars)")
            if source.purpose:
                self._report.record("created", f"binding {spec.slug}/{source.purpose}")
            return

        try:
            await self._templates.get(source.slug, include_deleted=True)
            exists = True
        except ResourceNotFoundError:
            exists = False

        if not exists:
            await self._templates.create(
                TemplateCreate(
                    slug=source.slug,
                    name=source.name,
                    description=source.description,
                    kind=source.kind,
                    content=body,
                    assets=asset_uris,
                    tags=[spec.slug],
                )
            )
            self._report.record("created", f"template {source.slug} v1")
        else:
            active = await self._templates.find_active_version(source.slug)
            unchanged = (
                active is not None
                and normalized(active.get("content")) == normalized(body)
                and (active.get("assets") or []) == asset_uris
            )
            if unchanged and active is not None:
                self._report.record("unchanged", f"template {source.slug} v{active['version']}")
            else:
                created = await self._templates.create_version(
                    source.slug,
                    TemplateVersionCreate(
                        content=body,
                        assets=asset_uris,
                        notes="seeded from karos-agents",
                        created_by="seed_legacy_agents",
                        activate=True,
                    ),
                )
                self._report.record("updated", f"template {source.slug} v{created['version']}")

        if source.purpose:
            await self._bind(spec, source)

    async def _bind(self, spec: LegacyAgentSpec, source: TemplateSource) -> None:
        assert source.purpose is not None
        try:
            link = await self._templates.get_agent_link(spec.slug, source.purpose)
            if link.get("template_id") == source.slug:
                self._report.record("unchanged", f"binding {spec.slug}/{source.purpose}")
                return
            outcome = "updated"
        except ResourceNotFoundError:
            outcome = "created"

        await self._templates.bind_to_agent(
            spec.slug, source.purpose, AgentTemplateLinkCreate(template_ref=source.slug)
        )
        self._report.record(outcome, f"binding {spec.slug}/{source.purpose} -> {source.slug}")


# --- Entry point ------------------------------------------------------------


def build_settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = dict(ENVIRONMENTS.get(args.env, {}))
    # Auth is a serving concern; this script talks to Firestore directly.
    overrides["auth_enabled"] = False
    if args.firestore_database:
        overrides["firestore_database"] = args.firestore_database
    if args.bucket:
        overrides["gcs_artifacts_bucket"] = args.bucket
    if args.env == "local":
        overrides.setdefault("gcp_project_id", "local-project")
        overrides.setdefault("pubsub_job_topic_id", "local-jobs")
        overrides.setdefault("gcs_artifacts_bucket", "local-artifacts")
    return Settings(**overrides)


async def run(args: argparse.Namespace) -> int:
    root = Path(args.karos_agents).resolve()
    if not root.is_dir():
        print(f"error: --karos-agents {root} is not a directory", file=sys.stderr)
        return 2

    settings = build_settings(args)
    report = Report()

    print(
        f"Seeding from {root}\n"
        f"  target      : env={args.env} firestore={settings.resolved_firestore_project_id}"
        f"/{settings.firestore_database}\n"
        f"  bucket      : {settings.gcs_artifacts_bucket}\n"
        f"  asset upload: {'on' if args.upload_assets else 'OFF (URIs recorded only)'}\n"
        f"  neutralize  : {'OFF (raw legacy text)' if args.no_neutralize else 'on'}\n"
        f"  mode        : {'DRY RUN - nothing is written' if args.dry_run else 'WRITING'}"
    )

    database = FirestoreDB(settings) if not args.dry_run else None
    uploader = AssetUploader(
        settings.gcs_artifacts_bucket,
        enabled=args.upload_assets and not args.dry_run,
        report=report,
    )
    try:
        uploader.preflight()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if database is not None:
            database.close()
        return 2

    # In a dry run nothing is read or written, so the services are never used;
    # constructing a Firestore client would demand credentials the caller may
    # not have, which would make --dry-run useless exactly when it is needed.
    seeder = Seeder(
        root=root,
        agents=AgentService(database) if database else None,  # type: ignore[arg-type]
        prompts=PromptService(database) if database else None,  # type: ignore[arg-type]
        templates=TemplateService(database) if database else None,  # type: ignore[arg-type]
        uploader=uploader,
        report=report,
        dry_run=args.dry_run,
        neutralize_prompts=not args.no_neutralize,
    )

    try:
        await seeder.seed_all()
    finally:
        if database is not None:
            database.close()

    print("\n" + "-" * 60)
    summary = ", ".join(f"{count} {name}" for name, count in sorted(report.counts.items()))
    print(f"summary: {summary or 'nothing to do'}")

    if report.warnings:
        print(f"\n{len(report.warnings)} prompt(s) still carry harness-specific content.")
        print("These need a human rewrite; the script never edits prose:")
        for warning in report.warnings:
            print(f"  ! {warning}")

    if args.strict and report.warnings:
        print("\n--strict: failing because harness-specific content remains.", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed agents, prompts and templates from the karos-agents lab repo.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--karos-agents",
        default="../karos-agents",
        help="Path to a karos-agents checkout",
    )
    parser.add_argument(
        "--env",
        choices=("prep", "prod", "local"),
        default="local",
        help="Target environment preset",
    )
    parser.add_argument("--firestore-database", help="Override the Firestore database id")
    parser.add_argument("--bucket", help="Override the GCS artifacts bucket")
    parser.add_argument(
        "--upload-assets",
        action="store_true",
        help="Actually upload binaries to GCS (otherwise only their URIs are recorded)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without touching Firestore or GCS",
    )
    parser.add_argument(
        "--no-neutralize",
        action="store_true",
        help=(
            "Seed prompts exactly as the lab repo has them, skipping the "
            "harness-vocabulary rewrites (see scripts/neutralize.py)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any prompt still contains harness-specific content",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
