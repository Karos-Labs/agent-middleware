"""FastAPI dependency providers.

Services are constructed once during application startup and stored on
``app.state``; these dependencies just hand them to request handlers, so a
handler never builds a Firestore or Pub/Sub client of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from app.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.db.firestore import FirestoreDB
from app.services.agents import AgentService
from app.services.client_context import ClientContextProjector
from app.services.context import ContextService
from app.services.dispatch import DispatchService
from app.services.engine_prompts import EnginePromptService
from app.services.feedback import FeedbackService
from app.services.models import ModelService
from app.services.prompt_store import UnifiedPromptStore
from app.services.prompts import PromptService
from app.services.runs import RunService
from app.services.templates import TemplateService


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> FirestoreDB:
    return request.app.state.db


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_projector(request: Request) -> ClientContextProjector | None:
    """The client-context projector, or None when no bucket is configured.

    Optional rather than absent: the two routes that need it answer 503 with
    the variable named, and everything else in the service keeps working.
    """

    projector: ClientContextProjector | None = request.app.state.projector
    return projector


def get_prompt_service(request: Request) -> PromptService:
    return request.app.state.prompt_service


def get_engine_prompt_service(request: Request) -> EnginePromptService:
    return request.app.state.engine_prompt_service


def get_prompt_store(request: Request) -> UnifiedPromptStore:
    """The append-only prompt store, or a 503 that says what is missing.

    Absent when there is no configuration database, which is a deployment
    state rather than a fault (S1). The legacy in-place write still works in
    that case -- see ``EnginePromptService.write`` -- so a Studio edit never
    stops working because Cloud SQL has not been stood up yet.
    """

    store: UnifiedPromptStore | None = getattr(request.app.state, "prompt_store", None)
    if store is None:
        raise ServiceUnavailableError(
            "the prompt store needs the configuration database (CONFIG_DB_DSN is "
            "unset in this environment), so version history and restore are not "
            "available here. Editing a prompt still works, on the legacy in-place "
            "path, which keeps at most 10 superseded revisions."
        )
    return store


def get_template_service(request: Request) -> TemplateService:
    return request.app.state.template_service


def get_model_service(request: Request) -> ModelService:
    return request.app.state.model_service


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


def get_feedback_service(request: Request) -> FeedbackService:
    return request.app.state.feedback_service


def get_context_service(request: Request) -> ContextService:
    return request.app.state.context_service


def get_dispatch_service(request: Request) -> DispatchService:
    return request.app.state.dispatch_service


async def resolve_agent(
    agent_id: str, agents: AgentService = Depends(get_agent_service)
) -> dict[str, Any]:
    """Load the agent named in the path, 404-ing before any nested work happens.

    Used by every nested route (prompts, examples, runs, feedback) so they never
    write into a subcollection of an agent that does not exist.
    """

    return await agents.get(agent_id)
