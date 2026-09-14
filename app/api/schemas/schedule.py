"""Schedules (S10 / SCRUM-223): definitions and the claim/settle protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Cadence = Literal["daily", "weekly", "monthly"]
ScheduleStatus = Literal["active", "paused", "completed"]


class ScheduleCreate(BaseModel):
    """Body for ``POST /clients/{client_slug}/schedules``.

    ``bill_client_credits`` is required. There is no default, because an
    unanswered "who pays" is the defect this whole ticket exists to close.
    """

    agent_slug: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    prompt: str = Field(default="", max_length=8000)
    cadence: Cadence
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    time_zone: str = Field(
        min_length=1, max_length=64, description="IANA zone, e.g. Asia/Jerusalem"
    )
    weekdays: list[int] | None = Field(
        default=None, description="weekly only. 0=Sunday .. 6=Saturday, the portal's convention"
    )
    day_of_month: int | None = Field(default=None, ge=1, le=31, description="monthly only")
    outputs_per_run: int = Field(default=1, ge=1, le=20)
    bill_client_credits: bool = Field(
        description="Whether each fire spends the client's credits. Stated, never inferred."
    )

    @model_validator(mode="after")
    def _cadence_fields_agree(self) -> ScheduleCreate:
        # The same rule the database enforces (schedules_cadence_fields_agree),
        # said here so the caller gets a 422 naming the field rather than a 409
        # naming a constraint.
        if self.cadence == "weekly":
            if not self.weekdays or not all(0 <= d <= 6 for d in self.weekdays):
                raise ValueError("a weekly schedule needs weekdays in 0..6")
            if self.day_of_month is not None:
                raise ValueError("a weekly schedule does not take day_of_month")
        elif self.cadence == "monthly":
            if self.day_of_month is None:
                raise ValueError("a monthly schedule needs day_of_month")
            if self.weekdays:
                raise ValueError("a monthly schedule does not take weekdays")
        elif self.weekdays or self.day_of_month is not None:
            raise ValueError("a daily schedule takes neither weekdays nor day_of_month")
        return self


class ScheduleRead(BaseModel):
    id: str
    client_slug: str
    agent_slug: str
    label: str
    prompt: str
    cadence: Cadence
    hour: int
    minute: int
    weekdays: list[int] | None
    day_of_month: int | None
    time_zone: str
    outputs_per_run: int
    bill_client_credits: bool
    billing_intent_source: Literal["explicit", "inferred_at_import"]
    status: ScheduleStatus
    next_run_at: datetime
    last_run_at: datetime | None
    last_job_id: str | None
    last_error: str | None
    last_error_at: datetime | None
    fire_in_flight_since: datetime | None
    fire_claim_id: str | None
    source_system: str | None
    source_id: str | None
    created_at: datetime
    updated_at: datetime


class ClaimedFire(ScheduleRead):
    """A schedule the caller now owns one fire of, plus what that fire is for."""

    fired_for: datetime = Field(description="The cursor this fire consumed (the slot that was due)")
    vanished_claim: bool = Field(
        description="True when an earlier claim never settled and this one supersedes it"
    )


class ClaimRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=200)


class ClaimResponse(BaseModel):
    """Every row here carries ``bill_client_credits``. There is no other way to get one."""

    claim_id: str
    fires: list[ClaimedFire]


class SettleRequest(BaseModel):
    """Body for ``POST /schedules/{id}/settle``. Exactly one of job_id / error."""

    claim_id: str
    job_id: str | None = Field(default=None, max_length=128)
    error: str | None = Field(default=None, max_length=2000)
    disable: bool = Field(
        default=False,
        description=(
            "Pause the schedule as well: the agent or client is gone and retrying helps nobody"
        ),
    )

    @model_validator(mode="after")
    def _one_outcome(self) -> SettleRequest:
        if (self.job_id is None) == (self.error is None):
            raise ValueError(
                "a settle reports either job_id (fired) or error (refused), not both or neither"
            )
        return self


class StatusChange(BaseModel):
    status: ScheduleStatus
