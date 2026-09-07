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

from app.api.routes import (
    agents,
    clients,
    configuration,
    context,
    engine_prompts,
    health,
    models,
    prompts,
    runs,
    templates,
)
from app.config import Settings, get_settings
from app.core.exceptions import (
    IncompleteAgentConfigurationError,
    InvalidStateError,
    MessagePublishError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    ValidationRefusedError,
)
from app.core.roles import Role
from app.db.firestore import FirestoreDB
from app.db.postgres import ConfigDatabase, build_config_database
from app.db.workspace import WorkspaceStore, build_workspace_store
from app.logging_config import configure_logging
from app.security import require_role, require_service_identity
from app.services.agents import AgentService
from app.services.client_context import ClientContextProjector
from app.services.configuration import ConfigurationService
from app.services.context import ContextService
from app.services.dispatch import DispatchService
from app.services.engine_prompts import EnginePromptService
from app.services.feedback import FeedbackService
from app.services.models import ModelService
from app.services.prompt_store import UnifiedPromptStore
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
    config_database: ConfigDatabase | None = None,
    workspace: WorkspaceStore | None = None,
) -> None:
    """Construct every service once and attach it to ``app.state``.

    Request handlers reach these through ``app.dependencies``, so no handler ever
    builds a Firestore or Pub/Sub client of its own. ``publisher`` and
    ``workspace`` are injectable so tests can wire fakes without patching
    module globals -- the same arrangement, and the same reason, as Pub/Sub.

    ``config_database`` is optional and stays optional: the Configuration API
    (S4) needs Cloud SQL, which does not exist in every environment yet, and a
    control plane that refused to start without it would make the Postgres
    migration a flag day for routes that have nothing to do with it. Its routes
    report 503 with the reason; everything else is unaffected.
    """

    publisher = publisher or PublisherService(settings)
    workspace = workspace if workspace is not None else build_workspace_store(settings)
    agent_service = AgentService(database)
    prompt_service = PromptService(database)
    prompt_store = (
        UnifiedPromptStore(config_database, database)
        if config_database is not None
        else None
    )
    engine_prompt_service = EnginePromptService(database, store=prompt_store)
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
    app.state.config_database = config_database
    app.state.configuration_service = (
        ConfigurationService(config_database) if config_database is not None else None
    )
    app.state.config_database = config_database
    app.state.prompt_store = prompt_store
    app.state.configuration_service = (
        ConfigurationService(config_database) if config_database is not None else None
    )
    # None when no bucket is configured, which is the local default. The two
    # routes that need it answer 503 naming the variable; nothing else cares.
    app.state.projector = (
        ClientContextProjector(database, workspace) if workspace is not None else None
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level)

    database = FirestoreDB(settings)
    config_database = await build_config_database(settings)
    build_services(app, settings, database, config_database=config_database)

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

    if settings.role_bindings_missing:
        # Loud, because nothing else surfaces it: authorization is switched off
        # while looking switched on. Every verified caller holds admin, exactly
        # as before roles existed, and the first binding is what makes the model
        # start enforcing.
        logger.error(
            "AUTH_ROLE_BINDINGS is empty while authentication is enabled: "
            "authorization is NOT being enforced and every verified caller holds "
            "admin. Bind the calling service accounts (AUTH_ROLE_BINDINGS) to turn "
            "it on; unbound callers then fall to AUTH_DEFAULT_ROLE=%s.",
            settings.auth_default_role.value,
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
    logger.info(
        "authorization: %s (%d role binding(s), default role %s)",
        "enforcing" if settings.auth_role_bindings else "NOT ENFORCING (no bindings)",
        len(settings.auth_role_bindings),
        settings.auth_default_role.value,
    )
    if config_database is not None:
        logger.info(
            "configuration database connected (pool %d-%d)",
            settings.config_db_pool_min_size,
            settings.config_db_pool_max_size,
        )

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app_name)
        app.state.publisher.close()
        database.close()
        if config_database is not None:
            await config_database.close()


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

    # Every protected route requires at least `viewer`; writes name a higher
    # minimum on the route itself. Authentication and the read floor belong
    # together here so a new router cannot be added without either.
    protected = [Depends(require_service_identity), Depends(require_role(Role.VIEWER))]
    app.include_router(agents.router, dependencies=protected)
    app.include_router(prompts.router, dependencies=protected)
    app.include_router(engine_prompts.router, dependencies=protected)
    app.include_router(templates.router, dependencies=protected)
    app.include_router(templates.agent_router, dependencies=protected)
    app.include_router(models.router, dependencies=protected)
    app.include_router(context.router, dependencies=protected)
    app.include_router(runs.router, dependencies=protected)
    app.include_router(runs.client_router, dependencies=protected)
    app.include_router(clients.router, dependencies=protected)
    app.include_router(configuration.router, dependencies=protected)

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

    @app.exception_handler(ValidationRefusedError)
    async def handle_validation_refused(
        _: Request, exc: ValidationRefusedError
    ) -> JSONResponse:
        """A publish that would produce a broken version.

        422 with every problem, not the first: the caller is meant to fix all
        of them in one pass. `detail` stays a string so a client that only
        renders that keeps working.
        """

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc), "problems": exc.problems},
        )

    @app.exception_handler(ServiceUnavailableError)
    async def handle_unavailable(_: Request, exc: ServiceUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)}
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
