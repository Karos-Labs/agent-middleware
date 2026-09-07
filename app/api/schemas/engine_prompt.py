"""Wire shapes for the prompt store agent-engine executes from."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EnginePromptRead(BaseModel):
    """One prompt version, as a stage would load it."""

    prompt_id: str
    version: str
    #: `promptId@version` — the exact string a stage's `skillRef` carries.
    skill_ref: str
    content: str
    #: True when the caller named a version rather than relying on `latestVersion`.
    #: Every compiled stage pins one, so this is normally true; it is surfaced
    #: because an unpinned read can move under you and a pinned one cannot.
    pinned: bool = True
    updated_at: Any | None = None
    updated_by: str | None = None


class EnginePromptUpdate(BaseModel):
    """Body for ``PUT /engine-prompts/{prompt_id}/versions/{version}``."""

    #: Refused when blank. A stage with no system prompt does not fail — it runs
    #: on its bare turn contract and produces plausible, unmoored output — so an
    #: empty save is the one edit that must not be allowed to look like success.
    content: str = Field(min_length=1)


class EnginePromptVersionSummary(BaseModel):
    """One superseded revision, kept so a bad edit can be read back."""

    version: str
    content: str
    replaced_at: Any | None = None
    replaced_by: str | None = None

# --- The unified store (S7 / SCRUM-221) -------------------------------------


class PromptVersionRead(BaseModel):
    """One version out of the append-only store.

    Distinct from :class:`EnginePromptVersionSummary`, which is a row of the
    capped ``supersededHistory`` array this replaces. That one had no version
    number of its own, no author you could rely on, and no way to be fetched
    individually -- so it could show you that an edit happened and never show
    you the edit.
    """

    version: int
    content: str
    content_hash: str
    #: authored -- written through this API. imported -- recovered from the
    #: engine document or from supersededHistory, which was capped at ten
    #: entries, so an import is a FLOOR on the history and not the whole of it.
    #: restored -- a copy of an earlier version.
    origin: str
    notes: str | None = None
    #: Which version this one reinstated, when it is a restore.
    restored_from_version: int | None = None
    #: True for the version currently sitting in the engine's document. Exactly
    #: one version is live, and it is not always the newest: a save whose
    #: projection failed leaves a newer version recorded but not running.
    is_live: bool = False
    created_at: Any | None = None
    created_by: str | None = None


class PromptRestoreRequest(BaseModel):
    """Body for ``POST /engine-prompts/{id}/versions/{v}/store/{n}/restore``."""

    reason: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Recorded on the version and on the audit row. A restore with no "
            "recorded reason is the one somebody re-litigates a week later."
        ),
    )
