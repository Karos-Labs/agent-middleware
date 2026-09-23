"""The learning loop's API bodies (C7 / SCRUM-461, 462, 463).

Field names are the engine's (camelCase, C7 §4.5) on everything the portal
reads back, because the portal renders what the run read and the two views
should be the same words. Request bodies accept the same spellings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Platform = Literal["x", "linkedin", "reddit", "instagram", "tiktok"]
Stage = Literal["attention", "expertise", "decide"]
FeedbackAction = Literal["posted", "posted_with_edits", "skipped", "change_requested", "note"]


class FileOutcomeRead(BaseModel):
    kind: str
    outcome: Literal["created", "updated", "unchanged", "skipped"]
    detail: str = ""
    rows: int | None = None


class LearningProjectionRead(BaseModel):
    """Body of ``POST /clients/{slug}/learning/{platform}/project``."""

    slug: str
    platform: str
    written: int
    files: list[FileOutcomeRead] = Field(default_factory=list)


class CollectRead(BaseModel):
    """Body of ``POST /runs/{run_id}/collect``."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    collected: bool
    reason: str = ""
    client_slug: str | None = Field(default=None, alias="clientSlug")
    platform: str | None = None
    subject_row_id: str | None = Field(default=None, alias="subjectRowId")
    record_changed: bool = Field(default=False, alias="recordChanged")
    platform_state_collected: bool = Field(default=False, alias="platformStateCollected")
    strategy_rows_collected: int = Field(default=0, alias="strategyRowsCollected")
    reprojected: int = 0


class LearningContextRead(BaseModel):
    """Body of ``GET /clients/{slug}/learning/{platform}``: what the next run reads."""

    model_config = ConfigDict(populate_by_name=True)

    platform: str
    settings: dict[str, Any]
    platform_state: dict[str, Any] | None = Field(default=None, alias="platform-state")
    subject_window: dict[str, Any] = Field(alias="subject-window")
    feedback: dict[str, Any]
    what_works: dict[str, Any] | None = Field(default=None, alias="what-works")
    strategy_map: dict[str, Any] | None = Field(default=None, alias="strategy-map")
    craft: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None
    #: N4. Derived from ``strategy-map`` and ``subject-window`` above, so a
    #: reader holding this payload can check the plan against what it was
    #: planned from. ``null`` means no map, not an empty calendar.
    sequence: dict[str, Any] | None = None


class FeedbackEventCreate(BaseModel):
    """Body of ``POST /clients/{slug}/learning/feedback`` -- one review action."""

    model_config = ConfigDict(populate_by_name=True)

    platform: Platform
    action: FeedbackAction
    run_id: str | None = Field(default=None, alias="runId", max_length=200)
    account: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=4000)
    original_text: str | None = Field(default=None, alias="originalText", max_length=20000)
    final_text: str | None = Field(default=None, alias="finalText", max_length=20000)
    actor: str | None = Field(default=None, max_length=255)
    at: datetime | None = None
    #: Set by an importer so the same portal event is never appended twice.
    source_id: str | None = Field(default=None, alias="sourceId", max_length=200)

    @model_validator(mode="after")
    def _an_edit_is_a_pair(self) -> FeedbackEventCreate:
        """The database refuses half an edit too; saying so here is a 422, not a 500."""

        if self.action == "posted_with_edits" and (
            self.original_text is None or self.final_text is None
        ):
            raise ValueError("posted_with_edits needs both originalText and finalText")
        return self


class FeedbackEventRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    row: dict[str, Any] | None
    duplicate: bool
    subject_rows_moved: int = Field(alias="subjectRowsMoved")
    reprojected: int


class FormatPreference(BaseModel):
    """One platform's post-type preference (0008). Every field optional."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    format: Literal["carousel", "single", "auto"] | None = None
    post_modes: list[Literal["news_flash"]] | None = Field(
        default=None, alias="postModes", max_length=4
    )
    picture_density: Literal["standard", "photo-first"] | None = Field(
        default=None, alias="pictureDensity"
    )
    series: str | None = Field(default=None, max_length=40, pattern=r"^[a-z_]+$")


class PreferencesWrite(BaseModel):
    """Body of ``PUT /clients/{slug}/learning/preferences`` -- the human half."""

    model_config = ConfigDict(populate_by_name=True)

    formats: dict[Platform, FormatPreference] | None = Field(
        default=None,
        description=(
            "Per-platform post-type preferences (2026-09-23). "
            "Replaces the stored map when given."
        ),
    )

    never_topics: list[str] | None = Field(default=None, alias="neverTopics", max_length=200)
    standing_instructions: list[str] | None = Field(
        default=None, alias="standingInstructions", max_length=200
    )
    updated_by: str | None = Field(default=None, alias="updatedBy", max_length=255)


class SettingsWrite(BaseModel):
    """Body of ``PUT /clients/{slug}/learning/{platform}/settings``."""

    model_config = ConfigDict(populate_by_name=True)

    anti_repeat_days: int | None = Field(default=None, alias="antiRepeatDays", ge=1, le=365)
    feedback_rows: int | None = Field(default=None, alias="feedbackRows", ge=1, le=200)
    sector: str | None = Field(default=None, max_length=64)


class StrategyRowWrite(BaseModel):
    id: str = Field(max_length=64)
    stage: Stage
    idea: str = Field(min_length=1, max_length=2000)
    problem: str | None = Field(default=None, max_length=2000)
    type: str | None = Field(default=None, max_length=64)
    evidence: str | None = Field(default=None, max_length=4000)
    status: Literal["open", "used", "retired"] | None = None


class StrategyMapWrite(BaseModel):
    """Body of ``PUT /clients/{slug}/learning/{platform}/strategy-map``."""

    model_config = ConfigDict(populate_by_name=True)

    source: Literal["setup-run", "first-run", "manual"] = "manual"
    built_at: datetime | None = Field(default=None, alias="builtAt")
    audience: list[dict[str, Any]] = Field(default_factory=list)
    default_mix: dict[str, int] | None = Field(default=None, alias="defaultMix")
    rows: list[StrategyRowWrite] = Field(default_factory=list, max_length=500)
    #: Retire every open row not named in ``rows``.
    replace: bool = False


class CraftRuleWrite(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(max_length=64, pattern=r"^L[123]-[a-z0-9][a-z0-9-]*$")
    platform: Platform
    layer: Literal["L1", "L2", "L3"]
    sector: str | None = Field(default=None, max_length=64)
    client_slug: str | None = Field(default=None, alias="clientSlug", max_length=128)
    kind: Literal["hard", "default"] = "default"
    rule: str = Field(min_length=1, max_length=2000)
    why: str | None = Field(default=None, max_length=2000)
    metric: str | None = Field(default=None, max_length=200)
    sample_size: int | None = Field(default=None, alias="sampleSize", ge=0)
    overrides: list[str] = Field(default_factory=list, max_length=50)
    status: Literal["active", "retired"] = "active"


class CraftRulesWrite(BaseModel):
    """Body of ``PUT /learning/craft-rules`` -- bulk upsert by id."""

    model_config = ConfigDict(populate_by_name=True)

    rules: list[CraftRuleWrite] = Field(min_length=1, max_length=500)
    updated_by: str | None = Field(default=None, alias="updatedBy", max_length=255)


class SubjectRowsRead(BaseModel):
    slug: str
    platform: str | None
    rows: list[dict[str, Any]]
