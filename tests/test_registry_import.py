"""The one-way import, against the real schema.

S5 / SCRUM-220. Three claims are worth a test each, and they are the three the
ticket makes rather than the ones that are easy to write.

**Reversible.** The ticket's own definition: "delete it and the documents are
still the source of truth." That is only true if the importer writes no
Firestore document at all, so that is asserted directly rather than reasoned
about.

**One-way and idempotent.** A second run must be a no-op. `prompt_versions` is
append-only by trigger, so an importer that appends an identical version on
every run grows the history forever and the only way back is a restore from
backup.

**It refuses rather than invents.** The interesting half. A step must satisfy
its kind, and a compiled workflow's stage list cannot -- so the importer's
value is in what it declines to make up.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import StagesAreCompiledError
from app.db.postgres import ConfigDatabase
from app.services.configuration import ConfigurationService
from app.services.prompt_store import UnifiedPromptStore
from app.services.registry_import import RegistryImporter
from app.services.snapshot import SnapshotResolver
from tests.conftest_postgres import requires_postgres
from tests.test_configuration_api import seed_reference_data

pytestmark = requires_postgres


# --- Fixtures ---------------------------------------------------------------


def _agent_document(slug: str, **overrides: Any) -> dict[str, Any]:
    """An `agents` document in the shape S-A16 actually writes."""

    document: dict[str, Any] = {
        "id": slug,
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": "Imported in a test.",
        "status": "enabled",
        "category": "social",
        "tags": ["content"],
        "credit_cost": 3,
        "is_public": True,
        "capabilities": ["draft_social_post"],
        "platforms": ["x"],
        "consumes_media": False,
        "supports_target_date": False,
        "custom_agent_keys": [f"karos-{slug}-v2"],
        "stages": [],
        "deleted_at": None,
    }
    document.update(overrides)
    return document


def _dynamic_stage(step_id: str, **overrides: Any) -> dict[str, Any]:
    """A stage from a dynamic spec: it carries its own prompt and schema."""

    stage: dict[str, Any] = {
        "id": step_id,
        "label": f"Stage {step_id}",
        "kind": "agent",
        "systemPrompt": f"You are the {step_id} stage. Write something.",
        "outputSchema": [{"name": "draft", "type": "string"}],
        "model_id": "claude-sonnet-4-6-on-vertex",
        "tools": ["read_client_context"],
    }
    stage.update(overrides)
    return stage


@pytest.fixture
def importer(
    config_database: ConfigDatabase, database: Any
) -> RegistryImporter:
    return RegistryImporter(
        config_database,
        database,
        ConfigurationService(config_database),
        UnifiedPromptStore(config_database, database),
    )


def _write(client: Any, collection: str, document: dict[str, Any]) -> None:
    client.documents[f"{collection}/{document['id']}"] = document


# --- Reversibility ----------------------------------------------------------


async def test_the_import_writes_no_firestore_document(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """The ticket's definition of reversible, asserted rather than argued.

    "Delete it and the documents are still the source of truth" holds only if
    the import never writes one. A snapshot of every document before and after
    is the whole test: if a future change starts projecting during an import,
    this fails, and it should -- the reverse of a projecting import is not a
    schema drop, it is a restore.
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    before = {path: dict(row) for path, row in fake_firestore_client.documents.items()}

    report = await importer.run(actor="test")

    assert report.counts()
    assert fake_firestore_client.documents == before


# --- The refusal that is the point -----------------------------------------


async def test_an_agent_whose_stages_are_compiled_gets_a_row_and_no_version(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """The thirteen hand-written workflows, and why this is not a shortfall.

    Their stage list is compiled TypeScript mirrored into Firestore for
    display: of 291 recorded stages, 27 carry a skillRef and none carries
    code. A step here must satisfy its kind, so importing them would mean
    inventing a prompt or a script for roughly 260 stages -- and S6 would then
    freeze the invention and hand it to the engine as a record of what ran.

    So: the agent row, its portal keys and its prompts (all configuration),
    and no version.
    """

    await seed_reference_data(config_database)
    _write(
        fake_firestore_client,
        "agents",
        _agent_document(
            "x-agent",
            stages=[
                # Exactly the shape `generate_engine_stages.py` produces: a
                # model step with a skillRef and no prompt body, and code
                # steps whose code is a program.
                {"id": "10-draft", "label": "Draft", "kind": "agent", "skill_ref": "x-draft@2"},
                {"id": "20-publish", "label": "Publish", "kind": "code"},
            ],
        ),
    )

    report = await importer.run(actor="test")

    (outcome,) = report.outcomes
    assert outcome.result == "row_only"
    assert outcome.stage_source == "engine_code"
    assert outcome.custom_agent_keys == 1

    row = await config_database.fetchrow(
        "select stage_source, published_version_id, source_registry from agents "
        "where slug = 'x-agent'"
    )
    assert row is not None
    assert row["stage_source"] == "engine_code"
    assert row["published_version_id"] is None
    assert row["source_registry"] == "middleware_agents"


async def test_the_database_refuses_to_publish_a_compiled_agent(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """0005's trigger, and why the rule is not left to the importer.

    An importer is one writer. The rule has to hold against the next one --
    a publish, a rollback, a hand-written UPDATE during an incident. Without
    the trigger the failure is quiet and late: the pointer moves, S6 freezes a
    step list that does not match the program about to run, and the message
    says it is a record of the configuration.
    """

    await seed_reference_data(config_database)
    _write(
        fake_firestore_client,
        "agents",
        _agent_document("x-agent", stages=[{"id": "10-draft", "kind": "agent"}]),
    )
    await importer.run(actor="test")

    with pytest.raises(Exception) as refused:
        await config_database.execute(
            "update agents set published_version_id = gen_random_uuid() "
            "where slug = 'x-agent'"
        )

    assert "engine_code" in str(refused.value)


async def test_the_resolver_says_stages_are_compiled_rather_than_unpublished(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """"Publish one" is impossible advice here, and impossible advice reads as a bug.

    The distinction is also what lets a dispatch fall back instead of refusing:
    `StagesAreCompiledError` is a subclass, so an API caller still gets 422
    while `DispatchService` catches this one specifically and publishes exactly
    what it published before.
    """

    await seed_reference_data(config_database)
    _write(
        fake_firestore_client,
        "agents",
        _agent_document("x-agent", stages=[{"id": "10-draft", "kind": "agent"}]),
    )
    await importer.run(actor="test")

    with pytest.raises(StagesAreCompiledError) as refused:
        await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert "compiled agent-engine workflow" in str(refused.value)
    assert "config_source" in str(refused.value)


async def test_a_dynamic_stage_missing_what_its_kind_needs_is_refused_with_every_reason(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """All the problems, not the first one.

    Same argument the publish validation makes: an author fixing an imported
    spec one refusal per run learns about problem two only after fixing
    problem one, which is how an importer acquires a reputation for being
    unpredictable while being perfectly consistent.
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {
            "id": "spec-1",
            "agentSlug": "x-agent",
            "stages": [
                # An ai stage with no prompt and no schema: two problems.
                {"id": "10-draft", "kind": "agent"},
                # A code stage with neither code nor a language: two more.
                {"id": "20-transform", "kind": "code"},
                # A gate with no kind.
                {"id": "30-review", "kind": "gate"},
            ],
        },
    )

    report = await importer.run(actor="test")

    (outcome,) = report.outcomes
    assert outcome.result == "refused"
    joined = " | ".join(outcome.reasons)
    assert "10-draft: an ai stage needs a prompt body" in joined
    assert "10-draft: an ai stage needs a non-empty output schema" in joined
    assert "20-transform: a code stage needs code" in joined
    assert "20-transform: a code stage needs a language" in joined
    assert "30-review: a gate needs a gate kind" in joined

    # And nothing was published on the strength of a partial reading.
    published = await config_database.fetchval(
        "select published_version_id from agents where slug = 'x-agent'"
    )
    assert published is None


# --- The happy path, for the two registries that have one -------------------


async def test_a_dynamic_spec_imports_as_a_published_version(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """The stages that really are data, imported and frozen.

    The inline `systemPrompt` is source number six in S7's list of six places
    a prompt lives, and the only one with no document of its own. It lands in
    `config.prompt_versions` like every other, which is what makes it
    editable, versioned and pinnable rather than a field on a spec.
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {
            "id": "spec-1",
            "agentSlug": "x-agent",
            "stages": [_dynamic_stage("10-draft")],
        },
    )

    report = await importer.run(actor="test")

    (outcome,) = report.outcomes
    assert outcome.result == "imported", outcome.reasons
    assert outcome.stage_source == "config"
    assert outcome.version == 1

    row = await config_database.fetchrow(
        "select stage_source, published_version_id, source_registry from agents "
        "where slug = 'x-agent'"
    )
    assert row is not None
    assert row["stage_source"] == "config"
    assert row["published_version_id"] is not None
    assert row["source_registry"] == "dynamicAgentSpecs"

    # The prompt body is in the store, with its provenance recorded.
    prompt = await config_database.fetchrow(
        "select p.prompt_key, p.source_registry, pv.content, pv.origin "
        "from prompts p join prompt_versions pv on pv.prompt_id = p.id "
        "where p.prompt_key = 'x-agent/10-draft'"
    )
    assert prompt is not None
    assert prompt["source_registry"] == "dynamicAgentSpecs"
    assert prompt["content"] == "You are the 10-draft stage. Write something."

    # And it is resolvable, which is the whole reason to import it.
    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")
    (step,) = snapshot.steps
    assert step.step_id == "10-draft"
    assert step.prompt is not None
    assert step.prompt.content == "You are the 10-draft stage. Write something."


# --- One-way and idempotent -------------------------------------------------


async def test_a_second_run_changes_nothing(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """The property that decides whether this is runnable on a schedule.

    Two things must not grow: agent versions (a fourth identical version of
    the same spec is noise a reader has to page through) and prompt versions
    (append-only by trigger, so growth here cannot be cleaned up -- only
    restored from backup).
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {"id": "spec-1", "agentSlug": "x-agent", "stages": [_dynamic_stage("10-draft")]},
    )

    first = await importer.run(actor="test")
    second = await importer.run(actor="test")

    assert [o.result for o in first.outcomes] == ["imported"]
    assert [o.result for o in second.outcomes] == ["unchanged"]

    assert (
        await config_database.fetchval(
            "select count(*) from agent_versions where agent_slug = 'x-agent'"
        )
        == 1
    )
    assert (
        await config_database.fetchval(
            "select count(*) from prompt_versions pv join prompts p on p.id = pv.prompt_id "
            "where p.prompt_key = 'x-agent/10-draft'"
        )
        == 1
    )


async def test_an_edited_spec_imports_as_a_new_version(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """Idempotence must not become inertness.

    The previous test would also pass if the importer simply never wrote
    twice. This is the other half: a genuine change produces a new version,
    and the earlier one is still there -- which is what makes a run dispatched
    before the edit still explicable afterwards.
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {"id": "spec-1", "agentSlug": "x-agent", "stages": [_dynamic_stage("10-draft")]},
    )
    await importer.run(actor="test")

    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {
            "id": "spec-1",
            "agentSlug": "x-agent",
            "stages": [_dynamic_stage("10-draft"), _dynamic_stage("20-verify")],
        },
    )
    second = await importer.run(actor="test")

    assert [o.result for o in second.outcomes] == ["imported"]
    assert [o.version for o in second.outcomes] == [2]
    versions = await config_database.fetch(
        "select version, status from agent_versions where agent_slug = 'x-agent' "
        "order by version"
    )
    assert [(v["version"], v["status"]) for v in versions] == [(1, "frozen"), (2, "frozen")]


# --- Portal keys ------------------------------------------------------------


async def test_a_portal_key_claimed_by_another_agent_is_a_conflict_not_a_move(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    """C4 §5 wants exactly one owner. Reassigning silently moves live traffic.

    The key is what the portal routes by, so an import that quietly repointed
    it would move a client's X drafting from one agent to another with no
    record beyond a changed row -- during a migration, which is exactly when
    nobody would attribute the change to the import.
    """

    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "agents",
        _agent_document("blog-agent", custom_agent_keys=["karos-x-agent-v2"]),
    )

    report = await importer.run(actor="test")

    results = {o.slug: o for o in report.outcomes}
    # Alphabetical order, so blog-agent claims the key first and x-agent is
    # the one refused. Either way exactly one of them is refused, and the
    # reason names the other.
    refused = [o for o in report.outcomes if o.result == "refused"]
    assert len(refused) == 1, {s: o.result for s, o in results.items()}
    assert "already claimed by agent" in " ".join(refused[0].reasons)

    owners = await config_database.fetch(
        "select custom_agent_key, agent_slug from agent_custom_agent_keys"
    )
    assert len(owners) == 1


# --- Dry run ----------------------------------------------------------------


async def test_a_dry_run_reports_and_writes_nothing(
    importer: RegistryImporter,
    config_database: ConfigDatabase,
    fake_firestore_client: Any,
) -> None:
    await seed_reference_data(config_database)
    _write(fake_firestore_client, "agents", _agent_document("x-agent"))
    _write(
        fake_firestore_client,
        "dynamicAgentSpecs",
        {"id": "spec-1", "agentSlug": "x-agent", "stages": [_dynamic_stage("10-draft")]},
    )

    report = await importer.run(actor="test", dry_run=True)

    assert [o.result for o in report.outcomes] == ["imported"]
    assert await config_database.fetchval("select count(*) from agents") == 0
    assert await config_database.fetchval("select count(*) from agent_versions") == 0
