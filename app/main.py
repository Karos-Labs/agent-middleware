"""FastAPI application entrypoint: wiring, lifespan management, exception handling.

The service is the **control plane** for the agent platform: the source of truth
in Firestore for agents, versioned system prompts, few-shot examples, content
templates (with their GCS asset bindings), runs and feedback.

It has exactly one outbound path — ``POST /agents/{agent_id}/jobs`` resolves an
agent's context, records a run, and publishes the payload to agent-engine's
topic. The engine is a stateless worker: everything it needs is in the message,
and it never reads this database.

There is deliberately no generic message-forwarding bridge. An earlier version
of this service relayed arbitrary Pub/Sub traffic between two topics (a pull
subscriber plus ``POST /pubsub/push``); that path knew nothing about agents,
recorded no run, and gave feedback nothing to attach to. Dispatch is the only
way a job reaches the engine, which is what keeps the run, the payload and the
message consistent by construction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.routes import agents, context, engine_prompts, health, models, prompts, runs, templates
from app.config import Settings, get_settings
from app.core.exceptions import (
    IncompleteAgentConfigurationError,
    InvalidStateError,
    MessagePublishError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.db.firestore import FirestoreDB
from app.logging_config import configure_logging
from app.security import require_service_identity
from app.services.agents import AgentService
from app.services.context import ContextService
from app.services.dispatch import DispatchService
from app.services.engine_prompts import EnginePromptService
from app.services.feedback import FeedbackService
from app.services.models import ModelService
from app.services.prompts import PromptService
from app.services.publisher import PublisherService
from app.services.runs import RunService
from app.services.templates import TemplateService

logger = logging.getLogger(__name__)


def build_services(
    app: FastAPI,
    settings: Settings,
    database: FirestoreDB,
    publisher: PublisherService | None = None,
) -> None:
    """Construct every service once and attach it to ``app.state``.

    Request handlers reach these through ``app.dependencies``, so no handler ever
    builds a Firestore or Pub/Sub client of its own. ``publisher`` is injectable
    so tests can wire a fake Pub/Sub client without patching module globals.
    """

    publisher = publisher or PublisherService(settings)
    agent_service = AgentService(database)
    prompt_service = PromptService(database)
    engine_prompt_service = EnginePromptService(database)
    template_service = TemplateService(database)
    model_service = ModelService(database)
    run_service = RunService(database)
    context_service = ContextService(settings, agent_service, prompt_service, template_service)

    app.state.settings = settings
    app.state.db = database
    app.state.publisher = publisher
    app.state.agent_service = agent_service
    app.state.prompt_service = prompt_service
    app.state.engine_prompt_service = engine_prompt_service
    app.state.template_service = template_service
    app.state.model_service = model_service
    app.state.run_service = run_service
    app.state.feedback_service = FeedbackService(database, run_service, prompt_service)
    app.state.context_service = context_service
    app.state.dispatch_service = DispatchService(
        settings, context_service, run_service, publisher, model_service
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level)

    database = FirestoreDB(settings)
    build_services(app, settings, database)

    if not settings.auth_enabled:
        logger.warning(
            "Service-to-service authentication is DISABLED (AUTH_ENABLED=false); "
            "every route is open to any caller that can reach this service"
        )
    elif settings.dev_token_permitted:
        logger.warning(
            "A static AUTH_DEV_TOKEN is configured and will be accepted alongside "
            "OIDC tokens (environment=%s)",
            settings.environment,
        )

    logger.info(
        "%s started (environment=%s, firestore=%s/%s, job_topic=%s, auth=%s)",
        settings.app_name,
        settings.environment,
        settings.resolved_firestore_project_id,
        settings.firestore_database,
        settings.job_topic_id,
        "enabled" if settings.auth_enabled else "disabled",
    )
    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app_name)
        app.state.publisher.close()
        database.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Middleware",
        description=(
            "Control plane for the agent platform: agents, versioned system prompts, "
            "few-shot examples, content templates and their GCS assets, run history, "
            "the feedback store, and the job-dispatch route that hands work to "
            "agent-engine."
        ),
        version="0.3.0",
        lifespan=lifespan,
    )

    # Health is deliberately unauthenticated: Cloud Run's startup and liveness
    # probes do not carry an identity token, so gating it would fail deploys.
    # It exposes only reachability booleans, never data.
    app.include_router(health.router)

    protected = [Depends(require_service_identity)]
    app.include_router(agents.router, dependencies=protected)
    app.include_router(prompts.router, dependencies=protected)
    app.include_router(engine_prompts.router, dependencies=protected)
    app.include_router(templates.router, dependencies=protected)
    app.include_router(templates.agent_router, dependencies=protected)
    app.include_router(models.router, dependencies=protected)
    app.include_router(context.router, dependencies=protected)
    app.include_router(runs.router, dependencies=protected)

    register_exception_handlers(app)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions onto HTTP responses, once, in one place."""

    @app.exception_handler(ResourceNotFoundError)
    async def handle_not_found(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
        )

    @app.exception_handler(ResourceConflictError)
    async def handle_conflict(_: Request, exc: ResourceConflictError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(InvalidStateError)
    async def handle_invalid_state(_: Request, exc: InvalidStateError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(IncompleteAgentConfigurationError)
    async def handle_incomplete_agent(
        _: Request, exc: IncompleteAgentConfigurationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
        )

    @app.exception_handler(ValidationError)
    async def handle_unreadable_document(_: Request, exc: ValidationError) -> JSONResponse:
        """A stored document that does not match this service's schema.

        Listings skip these (see ``parse_rows``); fetching one by id
        deliberately does not, because answering "here is that agent" with
        invented fields would be worse than failing. This turns what would
        otherwise be an unhandled traceback into something a reader can act on
        — the usual cause is another system writing to a collection this
        service also uses, which ``FIRESTORE_COLLECTION_PREFIX`` exists to
        prevent.
        """

        logger.error("Stored document failed validation: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": (
                    "A stored document does not match this service's schema. It was most "
                    "likely written by another system sharing this Firestore database."
                )
            },
        )

    @app.exception_handler(MessagePublishError)
    async def handle_publish_error(_: Request, exc: MessagePublishError) -> JSONResponse:
        logger.error("Publish error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Failed to publish message to Pub/Sub."},
        )


app = create_app()
