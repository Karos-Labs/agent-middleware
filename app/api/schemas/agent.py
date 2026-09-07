"""Agent CRUD schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.common import SlugStr
from app.api.schemas.presentation import (
    AgentInputDef,
    AgentReadiness,
    AgentStage,
)
from app.core.enums import AgentStatus, Capability


class AgentCreate(BaseModel):
    """Body for ``POST /agents``."""

    slug: SlugStr
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: AgentStatus = AgentStatus.ACTIVE
    agent_type: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    model_params: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # --- catalog / Studio presentation -------------------------------------
    # First-class rather than keys in `config`: config is opaque to this
    # service and passed through to the engine, so it is a private arrangement
    # between an agent and its workflow. What the catalog renders is a public
    # contract, and a schema is how a contract gets enforced.
    icon: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    #: Null means "platform default", not "free" — zero would be a decision.
    credit_cost: int | None = Field(default=None, ge=0)
    is_public: bool = True
    required_inputs: list[AgentInputDef] = Field(default_factory=list)
    stages: list[AgentStage] = Field(default_factory=list)
    #: True when `stages` describes compiled code, which is the case for every
    #: hand-written agent-engine workflow.
    stages_read_only: bool = True
    # --- C4 capability descriptor ------------------------------------------
    # What an agent is FOR, as opposed to how it presents. Written here once
    # and consumed by the portal's chat router, which selects on
    # capabilities x platforms x consumesMedia and never on the agent's name.
    capabilities: list[Capability] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    consumes_media: bool = False
    supports_target_date: bool = False
    custom_agent_keys: list[str] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    readiness: AgentReadiness = Field(default_factory=AgentReadiness)


class AgentUpdate(BaseModel):
    """Body for ``PATCH /agents/{agent_ref}``; unset fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: AgentStatus | None = None
    agent_type: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    model_params: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    tags: list[str] | None = None
    icon: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    credit_cost: int | None = Field(default=None, ge=0)
    is_public: bool | None = None
    required_inputs: list[AgentInputDef] | None = None
    stages: list[AgentStage] | None = None
    stages_read_only: bool | None = None
    capabilities: list[Capability] | None = None
    platforms: list[str] | None = None
    consumes_media: bool | None = None
    supports_target_date: bool | None = None
    custom_agent_keys: list[str] | None = None
    gates: list[str] | None = None
    readiness: AgentReadiness | None = None


class AgentStatusUpdate(BaseModel):
    """Body for ``PATCH /agents/{agent_ref}/status``."""

    status: AgentStatus


class AgentRead(BaseModel):
    """An agent as returned by the API."""

    id: str
    slug: str
    name: str
    description: str | None
    status: AgentStatus
    agent_type: str | None
    model: str | None
    model_params: dict[str, Any]
    config: dict[str, Any]
    tags: list[str]
    icon: str | None = None
    category: str | None = None
    credit_cost: int | None = None
    is_public: bool = True
    required_inputs: list[AgentInputDef] = Field(default_factory=list)
    stages: list[AgentStage] = Field(default_factory=list)
    stages_read_only: bool = True
    capabilities: list[Capability] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    consumes_media: bool = False
    supports_target_date: bool = False
    custom_agent_keys: list[str] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    readiness: AgentReadiness = Field(default_factory=AgentReadiness)
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
