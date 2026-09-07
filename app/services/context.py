"""Assembles the full context of an agent for injection into a job.

This is the endpoint the portal calls at launch time: one read that resolves the
agent's settings, its active system prompt, its active few-shot examples and the
active version of the template bound to the requested purpose. The engine then
works from that document alone -- it never talks to Firestore.
"""

from __future__ import annotations

import logging
from typing import Any

from app.api.schemas.context import (
    AgentContext,
    AgentContextAgent,
    AgentContextExample,
    AgentContextPrompt,
    AgentContextTemplate,
)
from app.api.schemas.presentation import AgentStage
from app.config import Settings
from app.core.exceptions import IncompleteAgentConfigurationError
from app.db.firestore import utcnow
from app.services.agents import AgentService
from app.services.prompts import PromptService
from app.services.templates import DEFAULT_PURPOSE, TemplateService

logger = logging.getLogger(__name__)


class ContextService:
    """Resolves everything dynamic about an agent into one snapshot."""

    def __init__(
        self,
        settings: Settings,
        agents: AgentService,
        prompts: PromptService,
        templates: TemplateService,
    ) -> None:
        self._settings = settings
        self._agents = agents
        self._prompts = prompts
        self._templates = templates

    async def build(
        self,
        agent_ref: str,
        *,
        purpose: str = DEFAULT_PURPOSE,
        template_ref: str | None = None,
        include_examples: bool = True,
        max_examples: int | None = None,
        require_active: bool = False,
    ) -> AgentContext:
        """Resolve the context of an agent.

        ``require_active`` refuses disabled agents; it is set when the context is
        about to become a real job, and left off for portal previews.
        """

        agent = (
            await self._agents.get_active(agent_ref)
            if require_active
            else await self._agents.get(agent_ref)
        )
        agent_id = agent["id"]

        prompt = await self._prompts.find_active(agent_id)
        limit = (
            max_examples
            if max_examples is not None
            else self._settings.default_context_example_limit
        )
        examples = await self._prompts.context_examples(agent_id, limit) if include_examples else []
        resolved_template = await self._templates.resolve_for_agent(
            agent_id, purpose=purpose, template_ref=template_ref
        )

        return AgentContext(
            agent=AgentContextAgent(
                id=agent_id,
                slug=agent["slug"],
                name=agent["name"],
                status=agent["status"],
                agent_type=agent.get("agent_type"),
                model=agent.get("model"),
                model_params=agent.get("model_params") or {},
                config=agent.get("config") or {},
                tags=agent.get("tags") or [],
                stages=[AgentStage.model_validate(st) for st in (agent.get("stages") or [])],
            ),
            system_prompt=_to_context_prompt(prompt),
            few_shot_examples=[_to_context_example(example) for example in examples],
            template=_to_context_template(resolved_template, purpose),
            resolved_at=utcnow(),
        )

    async def build_runnable(
        self,
        agent_ref: str,
        *,
        purpose: str = DEFAULT_PURPOSE,
        template_ref: str | None = None,
        include_examples: bool = True,
        max_examples: int | None = None,
    ) -> AgentContext:
        """Like :meth:`build`, but rejects a context that cannot produce a job."""

        context = await self.build(
            agent_ref,
            purpose=purpose,
            template_ref=template_ref,
            include_examples=include_examples,
            max_examples=max_examples,
            require_active=True,
        )
        if context.system_prompt is None and not _executes_from_engine_prompts(context.agent):
            raise IncompleteAgentConfigurationError(
                f"agent '{agent_ref}' has no active system prompt and no stage that "
                "resolves one from agent-engine's own prompt store; publish one before "
                "dispatching a job"
            )
        if context.system_prompt is None:
            # Not a warning: for an engine-resolved agent this is the normal,
            # correct state, and logging it as a problem would train readers to
            # ignore the line that matters (the raise above).
            logger.info(
                "Dispatching %s with no control-plane system prompt; its prompted stages "
                "resolve from agent-engine's prompt store",
                agent_ref,
            )
        return context


def _executes_from_engine_prompts(agent: AgentContextAgent) -> bool:
    """Whether this agent's prompted stages load their text from the engine.

    There are two prompt stores, and only one of them is what an agent runs on.
    This service owns ``agents/{slug}/prompts`` -- versioned, append-only, and
    resolved into :attr:`AgentContext.system_prompt`. agent-engine executes from
    its own root ``prompts``/``promptVersions`` collections, which a stage names
    through its ``skill_ref``. A stage carrying a ``skill_ref`` therefore has its
    prompt already, from a store this service does not resolve.

    That distinction used to cost nothing, because the gate above refused every
    agent with no active prompt here -- including the six seeded by
    ``scripts/seed_all_agents.py``, which deliberately leaves prompts absent for
    agents with no karos-agents lab source rather than "put words in a client's
    agent that no one wrote". Absent by design on one side, fatal on the other:
    ``intel-report-agent``, ``seo-geo-agent``, ``blog-agent``,
    ``newsletter-agent``, ``reputation-agent`` and ``branded-shorts-agent`` were
    all undispatchable, each failing with a 422 naming a document the run would
    not have read.

    Would not have read, today, in the strict sense: ``to_engine_message`` says
    the resolved prompt rides along for the engine to consume "once it knows
    how", and until then its ``RunJobRequestSchema`` keeps ``clientSlug``,
    ``productId`` and ``runKind`` and Zod strips the rest. The gate is kept
    rather than deleted anyway, and kept narrow rather than made advisory,
    because that day is a stated intention: an agent with no engine-side prompt
    source has nothing but this document to run on, and dispatching it would
    produce a run with no instructions at all. An agent with one loses nothing.
    """

    return any(stage.skill_ref for stage in agent.stages)


def _to_context_prompt(prompt: dict[str, Any] | None) -> AgentContextPrompt | None:
    if prompt is None:
        return None
    return AgentContextPrompt(
        id=prompt["id"],
        version=prompt["version"],
        content=prompt["content"],
        variables=prompt.get("variables") or [],
    )


def _to_context_example(example: dict[str, Any]) -> AgentContextExample:
    return AgentContextExample(
        id=example["id"],
        label=example.get("label"),
        user_input=example["user_input"],
        assistant_output=example["assistant_output"],
        tags=example.get("tags") or [],
    )


def _to_context_template(
    resolved: tuple[dict[str, Any], dict[str, Any]] | None, purpose: str
) -> AgentContextTemplate | None:
    if resolved is None:
        return None

    template, version = resolved
    return AgentContextTemplate(
        id=template["id"],
        slug=template["slug"],
        name=template["name"],
        kind=template["kind"],
        purpose=purpose,
        version_id=version["id"],
        version=version["version"],
        content=version.get("content"),
        schema_definition=version.get("schema_definition"),
        variables=version.get("variables") or [],
        # `or []` rather than a plain .get default: a version written before
        # assets existed has no key, and one written with an explicit null
        # should read back as empty rather than blowing up validation.
        assets=version.get("assets") or [],
    )
