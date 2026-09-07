"""System prompt and few-shot example schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import ExampleSource


class SystemPromptCreate(BaseModel):
    """Body for ``POST /agents/{agent_ref}/prompts``.

    Creating a prompt always creates a *new version*; existing versions are
    immutable. ``activate`` decides whether the new version becomes the one the
    context endpoint hands out.
    """

    content: str = Field(min_length=1)
    notes: str | None = Field(default=None, description="Changelog for this version")
    variables: list[str] = Field(default_factory=list)
    #: NOT accepted from the caller. `created_by` is stamped from the verified
    #: identity in the route, because a self-reported author on an audit record
    #: is decoration. Retained on `SystemPromptRead` below, which is where it is
    #: read back from.
    activate: bool = Field(default=True, description="Make this the active version")


class SystemPromptRead(BaseModel):
    """A single prompt version."""

    id: str
    agent_id: str
    version: int
    content: str
    notes: str | None
    variables: list[str]
    is_active: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class FewShotExampleCreate(BaseModel):
    """Body for ``POST /agents/{agent_ref}/examples``."""

    user_input: str = Field(min_length=1)
    assistant_output: str = Field(min_length=1)
    label: str | None = Field(default=None, max_length=255)
    prompt_id: str | None = Field(
        default=None, description="Pin this example to one prompt version (optional)"
    )
    extra: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)
    is_active: bool = True


class FewShotExampleUpdate(BaseModel):
    """Body for ``PATCH /agents/{agent_ref}/examples/{example_id}``."""

    user_input: str | None = Field(default=None, min_length=1)
    assistant_output: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, max_length=255)
    extra: dict[str, Any] | None = None
    tags: list[str] | None = None
    position: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class FewShotExampleRead(BaseModel):
    """A few-shot example as returned by the API."""

    id: str
    agent_id: str
    prompt_id: str | None
    label: str | None
    user_input: str
    assistant_output: str
    extra: dict[str, Any]
    tags: list[str]
    position: int
    is_active: bool
    source: ExampleSource
    source_run_id: str | None
    created_at: datetime
    updated_at: datetime
