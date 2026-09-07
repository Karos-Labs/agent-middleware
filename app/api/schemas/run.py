"""Run registration and feedback / evaluation schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import FeedbackStatus, RunStatus


class RunCreate(BaseModel):
    """Body for ``POST /agents/{agent_ref}/runs``.

    Used when the portal builds and publishes the payload itself and only needs
    the middleware to know the run exists, so feedback can be attached later.
    """

    run_id: str | None = Field(
        default=None,
        max_length=64,
        description="Portal-owned run id; a UUID4 is generated when omitted",
    )
    status: RunStatus = RunStatus.DISPATCHED
    job_type: str | None = Field(default=None, max_length=64)
    prompt_id: str | None = None
    prompt_version: int | None = Field(default=None, ge=1)
    template_version_id: str | None = None
    input_payload: dict[str, Any] = Field(default_factory=dict)
    pubsub_message_id: str | None = Field(default=None, max_length=128)
    requested_by: str | None = Field(default=None, max_length=255)
    client_slug: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Tenant this run belongs to. Optional here and required on dispatch: a "
            "caller that publishes its own payload already knows the client, but "
            "omitting it makes the run permanently unattributable -- nothing else "
            "on the document names a tenant."
        ),
    )


class RunUpdate(BaseModel):
    """Body for ``PATCH /agents/{agent_ref}/runs/{run_id}``.

    This is the engine result callback: status plus the produced artifact.
    """

    status: RunStatus | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    pubsub_message_id: str | None = Field(default=None, max_length=128)


class FeedbackCreate(BaseModel):
    """Body for ``POST /agents/{agent_ref}/runs/{run_id}/feedback``."""

    rating: int = Field(ge=1, le=5, description="1 (worst) to 5 (best)")
    status: FeedbackStatus
    correction_notes: str | None = Field(default=None, description="What should change")
    corrected_output: str | None = Field(
        default=None, description="The output as it should have been"
    )
    reviewer: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)


class FeedbackRead(BaseModel):
    """A stored reviewer verdict."""

    id: str
    run_id: str
    agent_id: str
    rating: int
    status: FeedbackStatus
    correction_notes: str | None
    corrected_output: str | None
    reviewer: str | None
    tags: list[str]
    promoted_example_id: str | None
    created_at: datetime
    updated_at: datetime


class RunRead(BaseModel):
    """A run, including any feedback it has collected."""

    id: str
    agent_id: str
    status: RunStatus
    job_type: str | None
    prompt_id: str | None
    prompt_version: int | None
    template_version_id: str | None
    input_payload: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    pubsub_message_id: str | None
    requested_by: str | None
    #: Null on every run dispatched before this field existed. Not backfillable:
    #: the run document's snapshot never carried a tenant, so there is nothing
    #: to recover it from on this side.
    client_slug: str | None = None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    feedback: list[FeedbackRead] = Field(default_factory=list)


class FeedbackExample(BaseModel):
    """A candidate few-shot example distilled from feedback.

    Returned by ``GET /agents/{agent_ref}/feedback/examples``: the reviewer's
    correction when there is one, otherwise the output the run produced.
    """

    feedback_id: str
    run_id: str
    rating: int
    status: FeedbackStatus
    user_input: str | None
    assistant_output: str | None
    correction_notes: str | None
    reviewer: str | None
    already_promoted: bool
    created_at: datetime


class FeedbackPromoteRequest(BaseModel):
    """Body for ``POST /agents/{agent_ref}/feedback/{feedback_id}/promote``."""

    label: str | None = Field(default=None, max_length=255)
    user_input: str | None = Field(
        default=None,
        description="Overrides the input recorded on the run, when it cannot be derived",
    )
    assistant_output: str | None = Field(
        default=None, description="Overrides the corrected output stored on the feedback"
    )
    tags: list[str] = Field(default_factory=list)
    position: int = Field(default=0, ge=0)
