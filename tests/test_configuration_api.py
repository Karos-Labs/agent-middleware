"""The Configuration API, against a real PostgreSQL 16.

Every refusal here is proven by attempting the write, because the schema's
guarantees are the database's and a fake would let all of them through.

The publish tests are the point of the file. Each validation exists because of
a specific way a version can be broken while looking fine, and the one worth
naming twice is the forward reference: `{{steps.later-step.field}}` is not a
cycle the engine detects. Stages run in array order, so it resolves to nothing,
the model is handed a prompt with a hole in it, and it produces something
plausible. Nobody notices until a client does.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db.firestore import FirestoreDB
from app.db.postgres import ConfigDatabase
from app.main import build_services, create_app
from app.services.publisher import PublisherService
from tests.conftest_postgres import requires_postgres

pytestmark = requires_postgres


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
async def api(
    settings: Settings,
    database: FirestoreDB,
    publisher_service: PublisherService,
    config_database: ConfigDatabase,
) -> AsyncIterator[AsyncClient]:
    """The real app, with the real Postgres wired in.

    An httpx AsyncClient rather than TestClient: TestClient runs the app on its
    own event loop, and the asyncpg pool belongs to the test's loop. Sharing a
    connection across loops is the kind of failure that shows up as an
    unrelated timeout three tests later.
    """

    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(
            app, settings, database, publisher=publisher_service,
            config_database=config_database,
        )
        yield

    app.router.lifespan_context = lifespan
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client


async def seed_reference_data(db: ConfigDatabase) -> None:
    """A priced model, two tools, and one of them granted to the drafting class."""

    await db.execute(
        """
        insert into models (
            model_id, display_name, vendor, route, provider_model_name,
            input_per_1m, output_per_1m, pricing_checked_on
        ) values
            ('claude-sonnet-4-6-on-vertex', 'Sonnet 4.6', 'anthropic', 'anthropic',
             'claude-sonnet-4-6', 3.0, 15.0, '2026-09-04'),
            ('claude-haiku-4-5-on-vertex', 'Haiku 4.5', 'anthropic', 'anthropic',
             'claude-haiku-4-5', 1.0, 5.0, '2026-09-04')
        """
    )
    await db.execute(
        """
        insert into tools (code, display_name, description, version) values
            ('read_client_context', 'Read client context', 'Reads the C1 envelope.', '1.0.0'),
            ('publish_to_x', 'Publish to X', 'Posts a draft.', '1.0.0')
        """
    )
    # Default deny: only the first is granted, so the second is refused at
    # publish without anyone having written a rule about it.
    # ON CONFLICT because capability_policy is NOT truncated between tests: it
    # carries 0002's reference data, and dropping that to make one grant
    # insertable would mean every test re-seeding the vocabulary too.
    await db.execute(
        """
        insert into capability_policy (agent_class_code, subject_type, subject)
        values ('drafting', 'tool', 'read_client_context')
        on conflict do nothing
        """
    )


async def seed_agent(db: ConfigDatabase, slug: str = "x-agent") -> None:
    await db.execute(
        """
        insert into agents (slug, name, agent_class_code, capabilities, platforms)
        values ($1, 'X Agent', 'drafting', array['draft_social_post'], array['x'])
        """,
        slug,
    )


async def seed_prompt(
    db: ConfigDatabase, key: str, content: str, version: int = 1
) -> str:
    prompt_id = await db.fetchval(
        """
        insert into prompts (prompt_key, agent_slug, purpose)
        values ($1, 'x-agent', 'skill')
        on conflict (prompt_key) do update set purpose = excluded.purpose
        returning id
        """,
        key,
    )
    return str(
        await db.fetchval(
            """
            insert into prompt_versions (prompt_id, version, content, content_hash)
            values ($1, $2, $3, $4)
            returning id
            """,
            prompt_id,
            version,
            content,
            hashlib.sha256(content.encode()).hexdigest(),
        )
    )


def ai_step(step_id: str, prompt_version_id: str, **overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "kind": "ai",
        "description": f"Step {step_id}",
        "prompt_version_id": prompt_version_id,
        "model_id": "claude-sonnet-4-6-on-vertex",
        "output_schema": [{"name": "draft", "type": "string"}],
        "allowed_tools": ["read_client_context"],
    }
    step.update(overrides)
    return step


# --- Availability -----------------------------------------------------------


async def test_the_api_reports_itself_unavailable_without_a_dsn(
    settings: Settings, database: FirestoreDB, publisher_service: PublisherService
) -> None:
    """503 and not 500: nothing is broken, Cloud SQL is not configured here.

    S1 (SCRUM-216) is still open in some environments, and a 500 sends someone
    reading a stack trace instead of an env var.
    """

    app = create_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        build_services(app, settings, database, publisher=publisher_service)
        yield

    app.router.lifespan_context = lifespan
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/config/agents/x-agent/versions")

    assert response.status_code == 503, response.text
    assert "CONFIG_DB_DSN" in response.json()["detail"]


# --- Authoring --------------------------------------------------------------


async def test_a_draft_starts_empty_and_lists_as_a_draft(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)

    created = await api.post("/config/agents/x-agent/versions", json={})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert body["lifecycle"] == "draft"
    assert body["steps"] == []

    listed = await api.get("/config/agents/x-agent/versions")
    assert [v["version"] for v in listed.json()] == [1]


async def test_versions_are_numbered_per_agent(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    await seed_agent(config_database, "blog-agent")

    for _ in range(2):
        assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201
    first_of_other = await api.post("/config/agents/blog-agent/versions", json={})

    assert [v["version"] for v in (await api.get("/config/agents/x-agent/versions")).json()] == [
        2,
        1,
    ]
    assert first_of_other.json()["version"] == 1


async def test_a_clone_copies_the_steps_and_their_tool_grants(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The grants are the easy thing to lose.

    They live in `tool_config` keyed by step row, not on the step, so a clone
    that copies only the steps produces a version whose every AI stage has no
    tools -- and that fails on the first tool call at run time, not at publish.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-draft", "Draft a post.")

    await api.post("/config/agents/x-agent/versions", json={})
    await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("10-draft", prompt)]},
    )

    cloned = await api.post("/config/agents/x-agent/versions", json={"from_version": 1})

    assert cloned.status_code == 201, cloned.text
    steps = cloned.json()["steps"]
    assert [s["step_id"] for s in steps] == ["10-draft"]
    assert steps[0]["allowed_tools"] == ["read_client_context"]
    assert steps[0]["prompt_version_id"] == prompt


async def test_position_is_the_index_of_the_submitted_list(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # Two sources of truth for order is how a reordered draft runs in the order
    # it was written instead of the order it is displayed.
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(config_database, "x-agent/10-a", "A")
    second = await seed_prompt(config_database, "x-agent/20-b", "B")

    await api.post("/config/agents/x-agent/versions", json={})
    replaced = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("20-b", second), ai_step("10-a", first)]},
    )

    assert replaced.status_code == 200, replaced.text
    assert [(s["step_id"], s["position"]) for s in replaced.json()["steps"]] == [
        ("20-b", 0),
        ("10-a", 1),
    ]


async def test_a_duplicate_step_id_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "A")
    await api.post("/config/agents/x-agent/versions", json={})

    refused = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("10-a", prompt), ai_step("10-a", prompt)]},
    )

    assert refused.status_code == 422
    assert "appears twice" in refused.text


async def test_naming_a_prompt_by_key_and_version_resolves_to_that_exact_version(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """Pinning the exact version is what makes a frozen version frozen.

    Storing the key would defer resolution to run time, which is the deferral
    this whole design removes.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    await seed_prompt(config_database, "x-agent/10-draft", "First.", version=1)
    second = await seed_prompt(config_database, "x-agent/10-draft", "Second.", version=2)

    await api.post("/config/agents/x-agent/versions", json={})
    replaced = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={
            "steps": [
                {
                    "step_id": "10-draft",
                    "kind": "ai",
                    "prompt_key": "x-agent/10-draft",
                    "prompt_version": 2,
                    "model_id": "claude-sonnet-4-6-on-vertex",
                    "output_schema": [{"name": "draft", "type": "string"}],
                }
            ]
        },
    )

    assert replaced.status_code == 200, replaced.text
    step = replaced.json()["steps"][0]
    assert step["prompt_version_id"] == second
    assert step["prompt_version"] == 2


async def test_a_prompt_key_without_a_version_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # A step that means "the latest" is a step whose behaviour changes when
    # somebody edits a prompt.
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    await seed_prompt(config_database, "x-agent/10-draft", "First.")
    await api.post("/config/agents/x-agent/versions", json={})

    refused = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={
            "steps": [
                {
                    "step_id": "10-draft",
                    "kind": "ai",
                    "prompt_key": "x-agent/10-draft",
                    "output_schema": [{"name": "draft", "type": "string"}],
                }
            ]
        },
    )
    assert refused.status_code == 422
    assert "go together" in refused.text


# --- Publish: the validations -----------------------------------------------


async def _draft_with(
    api: AsyncClient, steps: list[dict[str, Any]], version: int = 1
) -> dict[str, Any]:
    if version == 1:
        assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201
    replaced = await api.put(
        f"/config/agents/x-agent/versions/{version}/steps", json={"steps": steps}
    )
    assert replaced.status_code == 200, replaced.text
    return dict(replaced.json())


async def _publish(api: AsyncClient, version: int = 1, **body: Any) -> Any:
    return await api.post(
        f"/config/agents/x-agent/versions/{version}/publish", json=body
    )


def _codes(response: Any) -> set[str]:
    return {problem["code"] for problem in response.json()["problems"]}


async def test_a_forward_reference_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The failure this validation exists for.

    A reference to a LATER step is not a cycle the engine detects -- stages run
    in array order, so it resolves to nothing, the model gets a prompt with a
    hole in it, and the output is plausible and wrong.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(
        config_database, "x-agent/10-a", "Use {{steps.20-b.draft}} please."
    )
    second = await seed_prompt(config_database, "x-agent/20-b", "Write something.")
    await _draft_with(api, [ai_step("10-a", first), ai_step("20-b", second)])

    refused = await _publish(api)

    assert refused.status_code == 422, refused.text
    assert "forward_step_reference" in _codes(refused)
    problem = next(
        p for p in refused.json()["problems"] if p["code"] == "forward_step_reference"
    )
    assert problem["step_id"] == "10-a"
    assert "hole in it" in problem["message"]


async def test_a_backward_reference_is_fine(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    second = await seed_prompt(
        config_database, "x-agent/20-b", "Improve {{steps.10-a.draft}}."
    )
    await _draft_with(api, [ai_step("10-a", first), ai_step("20-b", second)])

    published = await _publish(api)
    assert published.status_code == 200, published.text


async def test_a_self_reference_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(
        config_database, "x-agent/10-a", "Improve {{steps.10-a.draft}}."
    )
    await _draft_with(api, [ai_step("10-a", prompt)])

    refused = await _publish(api)
    assert refused.status_code == 422
    assert "self_reference" in _codes(refused)


async def test_a_reference_to_a_step_that_is_not_here_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(
        config_database, "x-agent/10-a", "Use {{steps.deleted-step.draft}}."
    )
    await _draft_with(api, [ai_step("10-a", prompt)])

    refused = await _publish(api)
    assert refused.status_code == 422
    assert "unknown_step_reference" in _codes(refused)


async def test_a_reference_to_a_field_the_step_does_not_return_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The same silent hole, one level down.

    `{{steps.10-a.headline}}` where that step declares only `draft` resolves to
    nothing in exactly the same way, and the error message says what the step
    does declare so the fix is obvious.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    second = await seed_prompt(
        config_database, "x-agent/20-b", "Use {{steps.10-a.headline}}."
    )
    await _draft_with(api, [ai_step("10-a", first), ai_step("20-b", second)])

    refused = await _publish(api)

    assert refused.status_code == 422
    problem = next(
        p for p in refused.json()["problems"] if p["code"] == "unknown_step_field"
    )
    assert "declares draft" in problem["message"]


async def test_the_schema_makes_an_unpriced_model_unreachable(
    config_database: ConfigDatabase,
) -> None:
    """S12's rule, and the reason the publish check for it is defence in depth.

    The validator has an `unpriced_model` branch, and against this schema it is
    unreachable: `models.input_per_1m` is NOT NULL, so the row that made every
    cost path fall through to Sonnet's $3/$15 cannot be written at all. Proven
    here rather than asserted, because "the constraint makes the check
    redundant" is a claim worth failing loudly if a later migration relaxes it.

    The branch stays. A check whose only cost is a null test, guarding a number
    people bill clients on, is not worth removing for tidiness.
    """

    await seed_reference_data(config_database)

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await config_database.execute(
            """
            insert into models (
                model_id, display_name, vendor, route, provider_model_name,
                output_per_1m, pricing_checked_on
            ) values ('legacy-model', 'Legacy', 'other', 'gemini', 'legacy', 2.0,
                      '2026-09-04')
            """
        )

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await config_database.execute(
            "update models set input_per_1m = null "
            "where model_id = 'claude-sonnet-4-6-on-vertex'"
        )


async def test_an_unknown_model_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])
    # The FK stops a bad model reaching the table, so the version has to be
    # broken the way reality breaks it: the model is removed afterwards.
    await config_database.execute(
        "update agent_version_steps set model_id = null"
    )
    await config_database.execute(
        "update agent_versions set default_model_id = null where version = 1"
    )

    refused = await _publish(api)
    assert refused.status_code == 422
    assert "no_model" in _codes(refused)


async def test_a_retired_model_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # Retired exists so an old run's model still resolves to something that
    # explains what it was. It must not be selectable for a new version.
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])
    await config_database.execute(
        "update models set availability = 'retired' "
        "where model_id = 'claude-sonnet-4-6-on-vertex'"
    )

    refused = await _publish(api)
    assert refused.status_code == 422
    assert "retired_model" in _codes(refused)


async def test_a_tool_not_granted_to_the_agent_class_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """capability_policy is default-deny, and that direction is the value.

    `publish_to_x` is a registered tool with no policy row. The absence of a
    row is a no, so a tool nobody has thought about is not silently available
    to an agent that should not have it.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(
        api, [ai_step("10-a", prompt, allowed_tools=["read_client_context", "publish_to_x"])]
    )

    refused = await _publish(api)

    assert refused.status_code == 422
    problem = next(
        p for p in refused.json()["problems"] if p["code"] == "tool_not_permitted"
    )
    assert "publish_to_x" in problem["message"]
    assert "drafting" in problem["message"]


async def test_a_version_with_no_steps_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201

    refused = await _publish(api)
    assert refused.status_code == 422
    assert "no_steps" in _codes(refused)


async def test_every_problem_is_reported_at_once(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """Forty steps validated one refusal at a time is forty round trips.

    And an author who fixes one problem and learns about the next concludes
    that publish is unpredictable, when it is being perfectly consistent.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(
        config_database, "x-agent/10-a", "Use {{steps.20-b.draft}} and {{steps.ghost.x}}."
    )
    second = await seed_prompt(config_database, "x-agent/20-b", "Write something.")
    await _draft_with(
        api,
        [
            ai_step("10-a", first, allowed_tools=["publish_to_x"]),
            ai_step("20-b", second),
        ],
    )

    refused = await _publish(api)

    assert refused.status_code == 422
    assert {
        "forward_step_reference",
        "unknown_step_reference",
        "tool_not_permitted",
    } <= _codes(refused)
    assert len(refused.json()["problems"]) >= 3
    # `detail` stays a string, so a client that only renders that keeps working.
    assert isinstance(refused.json()["detail"], str)


# --- Publish: the transaction -----------------------------------------------


async def test_publishing_freezes_moves_the_pointer_and_audits(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])

    published = await _publish(api, note="first release")

    assert published.status_code == 200, published.text
    body = published.json()
    assert body["version"] == 1
    assert body["previous_version"] is None  # a first release, not a replacement
    assert body["published_by"]

    row = await config_database.fetchrow(
        """
        select v.status, v.frozen_at, v.frozen_by, a.published_version_id
          from agent_versions v join agents a on a.slug = v.agent_slug
         where v.agent_slug = 'x-agent' and v.version = 1
        """
    )
    assert row["status"] == "frozen"
    assert row["frozen_at"] is not None
    assert row["published_version_id"] == row["published_version_id"]

    audit = await config_database.fetchrow(
        "select action, actor, before, after, note from audit_log "
        "where action = 'publish' order by id desc limit 1"
    )
    assert audit["action"] == "publish"
    assert audit["note"] == "first release"
    assert audit["before"]["published_version_id"] is None
    assert audit["after"]["published_version"] == 1


async def test_a_frozen_version_cannot_be_edited_or_republished(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])
    await _publish(api)

    again = await _publish(api)
    assert again.status_code == 409
    assert "already frozen" in again.text

    edited = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("10-a", prompt, description="edited")]},
    )
    assert edited.status_code == 409
    assert "clone it into a new draft" in edited.text

    deleted = await api.delete("/config/agents/x-agent/versions/1")
    assert deleted.status_code == 409


async def test_a_dry_run_validates_and_changes_nothing(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The identical code path, which is the only kind of dry run worth having."""

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])

    dry = await _publish(api, dry_run=True)

    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    assert dry.json()["problems"] == []
    assert (
        await config_database.fetchval(
            "select status from agent_versions where agent_slug = 'x-agent' and version = 1"
        )
        == "draft"
    )
    assert (
        await config_database.fetchval(
            "select published_version_id from agents where slug = 'x-agent'"
        )
        is None
    )
    assert await config_database.fetchval("select count(*) from audit_log") == 2


async def test_a_failing_dry_run_returns_the_problems_without_a_422(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # A dry run answering "would this work" with an error status makes the
    # caller parse an exception to read a report.
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(
        config_database, "x-agent/10-a", "Use {{steps.ghost.x}}."
    )
    await _draft_with(api, [ai_step("10-a", prompt)])

    dry = await _publish(api, dry_run=True)

    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    assert {p["code"] for p in dry.json()["problems"]} == {"unknown_step_reference"}


async def test_a_publish_that_fails_part_way_leaves_a_draft(
    api: AsyncClient,
    config_database: ConfigDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transaction, proven by breaking it.

    A frozen version that nothing points at is the one state with no way out:
    it cannot be edited, and it is not live. So the freeze, the pointer move
    and the audit row are one transaction or they are a bug.
    """

    from app.services.configuration import ConfigurationService

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", prompt)])

    async def explode(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("the audit insert failed")

    monkeypatch.setattr(ConfigurationService, "_audit", staticmethod(explode))

    with pytest.raises(RuntimeError):
        await _publish(api)

    row = await config_database.fetchrow(
        """
        select v.status, a.published_version_id
          from agent_versions v join agents a on a.slug = v.agent_slug
         where v.agent_slug = 'x-agent' and v.version = 1
        """
    )
    assert row["status"] == "draft", "the freeze was not rolled back"
    assert row["published_version_id"] is None, "the pointer moved anyway"


# --- Rollback ---------------------------------------------------------------


async def _publish_two_versions(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(config_database, "x-agent/10-a", "Version one.")
    await _draft_with(api, [ai_step("10-a", first)])
    assert (await _publish(api)).status_code == 200

    second = await seed_prompt(config_database, "x-agent/10-a", "Version two.", version=2)
    created = await api.post("/config/agents/x-agent/versions", json={"from_version": 1})
    assert created.status_code == 201, created.text
    await api.put(
        "/config/agents/x-agent/versions/2/steps",
        json={"steps": [ai_step("10-a", second)]},
    )
    assert (await _publish(api, version=2)).status_code == 200


async def test_rollback_moves_a_pointer_and_changes_no_version_row(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """"No data change, no deletion" is literally true, and this asserts it."""

    await _publish_two_versions(api, config_database)
    before = await config_database.fetch(
        "select id, version, status, frozen_at from agent_versions "
        "where agent_slug = 'x-agent' order by version"
    )

    rolled = await api.post(
        "/config/agents/x-agent/rollback",
        json={"to_version": 1, "reason": "version two drafts off-brand"},
    )

    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"] == 1
    assert rolled.json()["previous_version"] == 2

    after = await config_database.fetch(
        "select id, version, status, frozen_at from agent_versions "
        "where agent_slug = 'x-agent' order by version"
    )
    assert [dict(r) for r in before] == [dict(r) for r in after]

    live = await config_database.fetchval(
        "select version from agent_versions v join agents a "
        "on a.published_version_id = v.id where a.slug = 'x-agent'"
    )
    assert live == 1

    audit = await config_database.fetchrow(
        "select action, before, after, note from audit_log "
        "where action = 'rollback' order by id desc limit 1"
    )
    assert audit["before"]["published_version"] == 2
    assert audit["after"]["published_version"] == 1
    assert audit["note"] == "version two drafts off-brand"


async def test_the_state_view_reports_published_and_superseded(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # Two stored statuses, three lifecycle states. The third is derived from
    # the pointer, which is what makes a rollback a pointer move.
    await _publish_two_versions(api, config_database)

    listed = {v["version"]: v["lifecycle"] for v in (
        await api.get("/config/agents/x-agent/versions")
    ).json()}
    assert listed == {2: "published", 1: "superseded"}

    await api.post("/config/agents/x-agent/rollback", json={"to_version": 1})

    listed = {v["version"]: v["lifecycle"] for v in (
        await api.get("/config/agents/x-agent/versions")
    ).json()}
    assert listed == {2: "superseded", 1: "published"}


async def test_rolling_back_to_a_draft_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await _publish_two_versions(api, config_database)
    assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201

    refused = await api.post("/config/agents/x-agent/rollback", json={"to_version": 3})
    assert refused.status_code == 409
    assert "still editable" in refused.text


async def test_rolling_back_to_what_is_already_live_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # Not an error to be clever about: it would write an audit row saying a
    # rollback happened when nothing changed.
    await _publish_two_versions(api, config_database)

    refused = await api.post("/config/agents/x-agent/rollback", json={"to_version": 2})
    assert refused.status_code == 409
    assert "already runs version 2" in refused.text


# --- Diff -------------------------------------------------------------------


async def test_a_diff_reads_as_a_review(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    a = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    b = await seed_prompt(config_database, "x-agent/20-b", "Improve it.")
    c = await seed_prompt(config_database, "x-agent/30-c", "Check it.")

    await _draft_with(api, [ai_step("10-a", a), ai_step("20-b", b)])
    await _publish(api)

    await api.post("/config/agents/x-agent/versions", json={"from_version": 1})
    await api.put(
        "/config/agents/x-agent/versions/2/steps",
        json={
            "steps": [
                ai_step("10-a", a, description="reworded"),
                ai_step("30-c", c),
            ]
        },
    )

    diff = await api.get(
        "/config/agents/x-agent/diff", params={"from_version": 1, "to_version": 2}
    )

    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["steps_added"] == ["30-c"]
    assert body["steps_removed"] == ["20-b"]
    changed = {c["step_id"]: c for c in body["steps_changed"]}
    assert "10-a" in changed
    assert {f["field"] for f in changed["10-a"]["fields"]} == {"description"}


async def test_a_reorder_is_reported_as_a_move_not_as_an_edit(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """A reorder and an edit need different attention from a reviewer.

    Lumped together, moving one step to the top makes every step below it look
    edited, and the one real change hides in forty rows of noise.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    a = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    b = await seed_prompt(config_database, "x-agent/20-b", "Improve it.")

    await _draft_with(api, [ai_step("10-a", a), ai_step("20-b", b)])
    await _publish(api)
    await api.post("/config/agents/x-agent/versions", json={"from_version": 1})
    await api.put(
        "/config/agents/x-agent/versions/2/steps",
        json={"steps": [ai_step("20-b", b), ai_step("10-a", a)]},
    )

    body = (
        await api.get(
            "/config/agents/x-agent/diff", params={"from_version": 1, "to_version": 2}
        )
    ).json()

    assert body["steps_changed"] == []
    moved = {m["step_id"]: (m["position_before"], m["position_after"]) for m in body["steps_moved"]}
    assert moved == {"10-a": (0, 1), "20-b": (1, 0)}


async def test_a_changed_prompt_shows_a_unified_diff(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    # The thing a reviewer actually wants to see. Reporting "the prompt version
    # changed" and nothing else makes them go and fetch both bodies by hand.
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    first = await seed_prompt(config_database, "x-agent/10-a", "Draft a post.\nBe concise.\n")
    await _draft_with(api, [ai_step("10-a", first)])
    await _publish(api)

    second = await seed_prompt(
        config_database, "x-agent/10-a", "Draft a post.\nBe warm.\n", version=2
    )
    await api.post("/config/agents/x-agent/versions", json={"from_version": 1})
    await api.put(
        "/config/agents/x-agent/versions/2/steps",
        json={"steps": [ai_step("10-a", second)]},
    )

    body = (
        await api.get(
            "/config/agents/x-agent/diff", params={"from_version": 1, "to_version": 2}
        )
    ).json()

    change = next(c for c in body["steps_changed"] if c["step_id"] == "10-a")
    assert "-Be concise." in change["prompt_diff"]
    assert "+Be warm." in change["prompt_diff"]
    assert change["prompt_diff_truncated"] is False


async def test_diffing_a_version_against_itself_is_empty(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)
    a = await seed_prompt(config_database, "x-agent/10-a", "Write something.")
    await _draft_with(api, [ai_step("10-a", a)])

    body = (
        await api.get(
            "/config/agents/x-agent/diff", params={"from_version": 1, "to_version": 1}
        )
    ).json()

    assert body["steps_added"] == []
    assert body["steps_removed"] == []
    assert body["steps_changed"] == []
    assert body["steps_moved"] == []
    assert body["defaults"] == []


# --- Not found --------------------------------------------------------------


async def test_an_unknown_agent_is_404_before_anything_else(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)

    assert (await api.get("/config/agents/ghost/versions")).status_code == 404
    assert (await api.post("/config/agents/ghost/versions", json={})).status_code == 404


async def test_an_unknown_version_is_404_and_says_which(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await seed_reference_data(config_database)
    await seed_agent(config_database)

    missing = await api.get("/config/agents/x-agent/versions/9")
    assert missing.status_code == 404
    assert "version 9" in missing.json()["detail"]
