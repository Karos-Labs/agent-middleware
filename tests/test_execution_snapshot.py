"""The ExecutionSnapshot resolver, against a real PostgreSQL.

S6 / SCRUM-219. The claim this file has to earn is narrow and load-bearing:
**a run executes the configuration it was dispatched with, and nothing else.**

Everything else in the snapshot is plumbing that a schema test would cover.
The three properties that are not plumbing each have a test here and are
worth naming, because each corresponds to a real failure the current
architecture allows:

1. *Resolved once* -- editing a prompt after dispatch does not change a queued
   run. Today it does, silently, and the run's own record says it used the
   version it never saw.
2. *Re-used on resume* -- a gate answered three days later executes against the
   snapshot the run started with. The longest-lived runs are the ones most
   likely to be edited mid-flight, so re-resolving on resume is the same bug
   with a longer fuse.
3. *Complete* -- if the consumer still has to look one thing up, the guarantee
   is void for that thing. So the prompt BODY travels, the model resolves to a
   provider name and a region, and the price travels with the model.

The database is real for the same reason it is real in `test_configuration_api`:
the resolver reads a schema whose refusals are the product, and a fake
repository would answer every query with whatever the test wanted.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import asyncpg
import pytest
from httpx import AsyncClient

from app.core.exceptions import IncompleteAgentConfigurationError, ResourceNotFoundError
from app.db.postgres import ConfigDatabase
from app.services.snapshot import (
    INLINE_LIMIT_BYTES,
    SnapshotResolver,
    SnapshotTransport,
    snapshot_message_fields,
    wire_size,
)
from tests.conftest import FakeWorkspaceStore
from tests.conftest_postgres import requires_postgres
from tests.test_configuration_api import (
    ai_step,
    seed_agent,
    seed_prompt,
    seed_reference_data,
)

pytestmark = requires_postgres


# --- Helpers ----------------------------------------------------------------


async def _publish_one_step(
    api: AsyncClient,
    db: ConfigDatabase,
    content: str = "Draft a post about {{topic}}.",
    **step_overrides: Any,
) -> None:
    """A published, frozen, one-step version of `x-agent`."""

    await seed_reference_data(db)
    await seed_agent(db)
    prompt = await seed_prompt(db, "x-agent/10-draft", content)
    assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201
    replaced = await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("10-draft", prompt, **step_overrides)]},
    )
    assert replaced.status_code == 200, replaced.text
    published = await api.post("/config/agents/x-agent/versions/1/publish", json={})
    assert published.status_code == 200, published.text


async def _publish_second_version(
    api: AsyncClient, db: ConfigDatabase, content: str
) -> None:
    prompt = await seed_prompt(db, "x-agent/10-draft", content, version=2)
    created = await api.post("/config/agents/x-agent/versions", json={"from_version": 1})
    assert created.status_code == 201, created.text
    replaced = await api.put(
        "/config/agents/x-agent/versions/2/steps",
        json={"steps": [ai_step("10-draft", prompt)]},
    )
    assert replaced.status_code == 200, replaced.text
    assert (
        await api.post("/config/agents/x-agent/versions/2/publish", json={})
    ).status_code == 200


# --- Completeness -----------------------------------------------------------


async def test_a_snapshot_carries_everything_the_engine_would_have_looked_up(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The prompt body, the provider model name, the price, the tool version.

    Each of these is something the engine reads from somewhere else today, and
    each `assert` here is one fewer read on the execution path. The price is
    the one that is easy to leave out and expensive to leave out: without it
    the engine costs the run against its own hard-coded table, which falls back
    to Sonnet's $3/$15 on a miss and bills Opus at a third of its cost.
    """

    await _publish_one_step(api, config_database, "Draft a post about {{topic}}.")

    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert snapshot.agent_slug == "x-agent"
    assert snapshot.agent_version == 1
    assert snapshot.client_slug == "acme"
    assert snapshot.resolved_from == "published"
    assert snapshot.agent_class == "drafting"

    (step,) = snapshot.steps
    assert step.step_id == "10-draft"
    assert step.kind == "ai"
    assert step.prompt is not None
    # The body, not a pointer to it.
    assert step.prompt.content == "Draft a post about {{topic}}."
    assert step.prompt.content_hash

    assert step.model is not None
    assert step.model.model_id == "claude-sonnet-4-6-on-vertex"
    assert step.model.provider_model_name == "claude-sonnet-4-6"
    assert step.model.pricing.input_per_1m == 3.0
    assert step.model.pricing.output_per_1m == 15.0

    assert [tool.name for tool in step.allowed_tools] == ["read_client_context"]
    assert step.allowed_tools[0].version == "1.0.0"


async def test_a_pinned_provider_policy_carries_no_fallback_model(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The engine refuses a fallback declared alongside `pinned`.

    Emitting one anyway would produce a snapshot the consumer rejects, which is
    a worse failure than the one it was trying to be helpful about.
    """

    await _publish_one_step(api, config_database)

    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert snapshot.steps[0].model is not None
    assert snapshot.steps[0].model.provider_policy == "pinned"
    assert snapshot.steps[0].model.fallback_model is None


# --- Property 1: resolved once ----------------------------------------------


async def test_publishing_a_new_version_does_not_change_a_snapshot_already_taken(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The whole point of the ticket, as one assertion.

    A run dispatched at 10:00 and executed at 10:05, with an author publishing
    at 10:02, executes what it was dispatched with. Today the engine re-reads
    the prompt at execution time and the run's own record names a version it
    never used, which makes every piece of feedback on it unattributable.
    """

    await _publish_one_step(api, config_database, "The original instruction.")
    resolver = SnapshotResolver(config_database)

    taken = await resolver.resolve("x-agent", "acme")
    assert taken.steps[0].prompt is not None
    assert taken.steps[0].prompt.content == "The original instruction."

    await _publish_second_version(api, config_database, "A completely different one.")

    # The object in flight is untouched...
    assert taken.steps[0].prompt.content == "The original instruction."
    assert taken.agent_version == 1
    # ...and a dispatch made after the publish gets the new one, so this is
    # immunity rather than staleness.
    later = await resolver.resolve("x-agent", "acme")
    assert later.agent_version == 2
    assert later.steps[0].prompt is not None
    assert later.steps[0].prompt.content == "A completely different one."


async def test_a_snapshot_is_reusable_verbatim_after_a_publish(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """Property 2, as the resume path actually uses it: serialise, then reload.

    A resumed run does not hold the Python object -- it reads the snapshot off
    the message or out of the run document. So the test that matters is that
    the round trip through JSON survives a configuration change, with zero
    further reads of the database.
    """

    await _publish_one_step(api, config_database, "The instruction at dispatch.")
    resolver = SnapshotResolver(config_database)
    on_the_wire = (await resolver.resolve("x-agent", "acme")).model_dump_json(by_alias=True)

    await _publish_second_version(api, config_database, "Edited while the gate waited.")

    # Three days later, the gate is answered. Nothing is read.
    resumed = json.loads(on_the_wire)
    assert resumed["agentVersion"] == 1
    assert resumed["steps"][0]["prompt"]["content"] == "The instruction at dispatch."


# --- Resolution order -------------------------------------------------------


async def test_a_client_pin_beats_the_published_pointer(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await _publish_one_step(api, config_database, "Version one.")
    await _publish_second_version(api, config_database, "Version two.")

    first_id = await config_database.fetchval(
        "select id from agent_versions where agent_slug = 'x-agent' and version = 1"
    )
    await config_database.execute(
        "insert into client_agent_config (client_slug, agent_slug, pinned_version_id) "
        "values ('acme', 'x-agent', $1)",
        first_id,
    )

    resolver = SnapshotResolver(config_database)
    pinned = await resolver.resolve("x-agent", "acme")
    unpinned = await resolver.resolve("x-agent", "globex")

    assert (pinned.agent_version, pinned.resolved_from) == (1, "client_pinned")
    assert (unpinned.agent_version, unpinned.resolved_from) == (2, "published")


async def test_a_disabled_client_falls_back_to_the_published_version(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """`enabled = false` is not "run version one anyway"."""

    await _publish_one_step(api, config_database, "Version one.")
    await _publish_second_version(api, config_database, "Version two.")
    first_id = await config_database.fetchval(
        "select id from agent_versions where agent_slug = 'x-agent' and version = 1"
    )
    await config_database.execute(
        "insert into client_agent_config (client_slug, agent_slug, pinned_version_id, enabled) "
        "values ('acme', 'x-agent', $1, false)",
        first_id,
    )

    resolved = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert (resolved.agent_version, resolved.resolved_from) == (2, "published")


async def test_a_stage_model_override_is_applied_and_then_frozen(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """C6 §9.3: the override is applied HERE, once.

    The message already carries a per-stage model. If the snapshot also
    resolved the step's own model, the consumer would need a merge rule for two
    fields deciding one thing -- and the last feature that shipped with two
    such fields (the per-stage model picker) did nothing at all for months.
    """

    await _publish_one_step(api, config_database)

    resolved = await SnapshotResolver(config_database).resolve(
        "x-agent", "acme", stage_models={"10-draft": "claude-haiku-4-5-on-vertex"}
    )

    assert resolved.steps[0].model is not None
    assert resolved.steps[0].model.model_id == "claude-haiku-4-5-on-vertex"
    # Resolved, not merely named: the price came along with the substitution.
    assert resolved.steps[0].model.pricing.input_per_1m == 1.0


# --- Refusals ---------------------------------------------------------------


async def test_an_unknown_agent_is_not_found(config_database: ConfigDatabase) -> None:
    with pytest.raises(ResourceNotFoundError):
        await SnapshotResolver(config_database).resolve("no-such-agent", "acme")


async def test_an_agent_with_only_a_draft_is_refused_rather_than_run(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """A draft is editable, and the contract is that a running config is not.

    Falling back to the draft would make the snapshot a snapshot of something
    still moving, which is the failure this whole ticket exists to remove.
    """

    await seed_reference_data(config_database)
    await seed_agent(config_database)
    prompt = await seed_prompt(config_database, "x-agent/10-draft", "Draft.")
    assert (await api.post("/config/agents/x-agent/versions", json={})).status_code == 201
    await api.put(
        "/config/agents/x-agent/versions/1/steps",
        json={"steps": [ai_step("10-draft", prompt)]},
    )

    with pytest.raises(IncompleteAgentConfigurationError) as refused:
        await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert "no published version" in str(refused.value)


async def test_a_stage_override_naming_an_unknown_model_is_refused(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The one way a snapshot can reach an unpriceable model, and it is refused.

    A step's own model cannot vanish from under a frozen version --
    `agent_version_steps.model_id` is a foreign key, so the delete is refused
    at the database and the "catalog changed after freezing" branch is
    unreachable through that door. The override in the dispatch request is a
    different matter: it arrives from the portal and nothing constrains it, so
    it is the door this check exists for.

    Refusing beats resolving without a price. The alternative is a run whose
    cost cannot be computed, and an uncomputable cost does not surface as an
    error -- it surfaces as a plausible wrong number in a report.
    """

    await _publish_one_step(api, config_database)

    with pytest.raises(IncompleteAgentConfigurationError) as refused:
        await SnapshotResolver(config_database).resolve(
            "x-agent", "acme", stage_models={"10-draft": "claude-opus-9-imaginary"}
        )

    assert "no longer in the catalog" in str(refused.value)
    assert "claude-opus-9-imaginary" in str(refused.value)


async def test_the_database_refuses_to_delete_a_model_a_frozen_version_uses(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """Why the branch above cannot be reached through the step's own model.

    Asserted rather than assumed: this is the constraint that makes a frozen
    version's pricing durable, and a schema change that dropped it would
    otherwise turn a refused delete into a run nobody can cost.
    """

    await _publish_one_step(api, config_database)

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await config_database.execute(
            "delete from models where model_id = 'claude-sonnet-4-6-on-vertex'"
        )


# --- Transport --------------------------------------------------------------


async def test_a_normal_snapshot_travels_inline(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """Measured, not assumed: the realistic case is nowhere near the ceiling."""

    await _publish_one_step(api, config_database)
    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    assert wire_size(snapshot) < INLINE_LIMIT_BYTES // 100
    assert SnapshotTransport(FakeWorkspaceStore()).fits_inline(snapshot)

    fields = snapshot_message_fields(snapshot, None)
    assert fields["snapshotId"] == snapshot.snapshot_id
    assert fields["snapshot"]["agentSlug"] == "x-agent"
    # Exactly one of the two, always: a consumer handed both has to choose, and
    # whichever it picks is somebody's surprise.
    assert "snapshotUri" not in fields


async def test_a_snapshot_over_the_limit_is_offloaded_and_travels_by_uri(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    """The threshold crossing, forced with a small limit rather than a big agent.

    Building a genuinely 5MB snapshot would mean a fixture that spends seconds
    on DDL to prove an inequality. The limit is a constructor argument for
    exactly this reason.
    """

    await _publish_one_step(api, config_database)
    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    workspace = FakeWorkspaceStore()
    transport = SnapshotTransport(workspace, limit=10)
    assert not transport.fits_inline(snapshot)

    reference = await transport.offload(snapshot)

    assert reference.uri == f"gs://test-artifacts/snapshots/{snapshot.snapshot_id}.json.gz"
    assert reference.snapshot_id == snapshot.snapshot_id
    # The object is the snapshot, gzipped -- not a summary of it.
    (path,) = workspace.writes
    restored = json.loads(gzip.decompress(workspace.blobs[path]))
    assert restored["snapshotId"] == snapshot.snapshot_id
    assert restored["steps"][0]["prompt"]["content"]

    fields = snapshot_message_fields(snapshot, reference)
    assert fields["snapshotUri"] == reference.uri
    assert fields["snapshotSha256"] == reference.sha256
    assert "snapshot" not in fields


async def test_an_oversized_snapshot_with_no_bucket_says_which_variable_is_unset(
    api: AsyncClient, config_database: ConfigDatabase
) -> None:
    await _publish_one_step(api, config_database)
    snapshot = await SnapshotResolver(config_database).resolve("x-agent", "acme")

    with pytest.raises(IncompleteAgentConfigurationError) as refused:
        await SnapshotTransport(None, limit=10).offload(snapshot)

    assert "GCS_ARTIFACTS_BUCKET" in str(refused.value)
