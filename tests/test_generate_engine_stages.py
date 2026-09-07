"""The stage extractor, on the source shapes agent-engine actually uses.

``engine_stages.json`` is what the Studio shows and what dispatch keys a
per-stage model override by, so the extractor has to read every shape a
workflow author has reached for -- not just the bare-literal id the first
version understood. Each case here is a shape found in a real workflow on
2026-09-07, and each was invisible to the extractor before that day: the
committed stage file listed 25 model steps when the engine ran 39.
"""

from __future__ import annotations

from pathlib import Path

from scripts.generate_engine_stages import (
    _policy_from_text,
    class_model_policies,
    class_skill_refs,
    second_argument,
    stages_for_workflow,
)

WORKFLOW = """
export function createDemoWorkflow(deps) {
  return async (wf) => {
    await wf.step.code("00-intake-check", async () => 1);
    const draftAgent = new DemoDraftAgent({ router: deps.router });
    const rev = (id: string) => id;
    const att = (id: string) => id;
    for (let attempt = 1; attempt <= 3; attempt++) {
      const draftResult = await wf.step.agent(rev(att("10-draft-post")), draftAgent, {});
    }
    await wf.step.code(rev("11-verify-numbers-sourced"), async () => 1);
    const agent = new DemoScriptAgent({ router: deps.router });
    await wf.step.code("03s-script", async () => {
      const exec = await wf.step.agent(stepId.replace("03s-script", "03u-script"), agent, {});
    });
    const judge = new DemoJudgeAgent({ router: deps.router });
    await wf.step.agent(`08-craft-verdict${suffix}`, judge, { draft });
    await wf.step.code(rev(`05-write-copy-attempt-${attempt}`), async () => 1);
    await wf.step.gate("15-batch-review", { kind: "batch_review" });
  };
}
"""

AGENT_SOURCE = """
import { BaseAgent, resolveModelPolicy } from "@agent-engine/core";
export const DEMO_STEP_ID = "demo-draft";
export const DEMO_MODEL_ID = "claude-haiku-4-5-20251001";
export const DEMO_POLICY = resolveModelPolicy(DEMO_STEP_ID, {
  policy: "pinned",
  model: "claude-opus-4-8",
});
export class DemoDraftAgent extends BaseAgent<Out> {
  protected readonly config = {
    id: DEMO_STEP_ID,
    allowedTools: [],
    // a long comment
    modelPolicy: DEMO_POLICY,
    skillRef: "demo-craft@3",
  };
}
export class DemoScriptAgent extends BaseAgent<Out> {
  protected readonly config = {
    id: "demo-script",
    modelPolicy: resolveModelPolicy("demo-script", {
      policy: "pinned",
      model: "gemini-2.5-pro",
      vendor: "gemini",
    }),
    skillRef: "demo-script@1",
  };
}
export class DemoJudgeAgent extends BaseAgent<Out> {
  protected readonly config = {
    id: "demo-judge",
    modelPolicy: { policy: "commodity", model: DEMO_MODEL_ID },
  };
}
"""


def _engine(tmp_path: Path) -> Path:
    root = tmp_path / "agent-engine"
    agent_dir = root / "agents" / "demo-agent" / "src"
    (agent_dir / "agent").mkdir(parents=True)
    (agent_dir / "workflow").mkdir(parents=True)
    (agent_dir / "agent" / "demo-agents.ts").write_text(AGENT_SOURCE, encoding="utf-8")
    (agent_dir / "workflow" / "create-demo-workflow.ts").write_text(WORKFLOW, encoding="utf-8")
    return root


def test_second_argument_skips_commas_inside_the_id_expression() -> None:
    text = 'wf.step.agent(stepId.replace("a", "b"), agent, {})'
    after = second_argument(text, text.index("("))
    assert after is not None
    assert text[after:].lstrip().startswith("agent,")


def test_policies_follow_constants_for_the_id_the_policy_and_the_model(tmp_path: Path) -> None:
    policies = class_model_policies(_engine(tmp_path))
    assert policies["DemoDraftAgent"] == {
        "agent_id": "demo-draft",
        "default_model": "claude-opus-4-8",
        "vendor": "anthropic",
    }
    assert policies["DemoScriptAgent"] == {
        "agent_id": "demo-script",
        "default_model": "gemini-2.5-pro",
        "vendor": "gemini",
    }
    # A literal policy whose `model:` is a constant.
    assert policies["DemoJudgeAgent"]["default_model"] == "claude-haiku-4-5-20251001"


def test_policy_text_without_a_model_is_none() -> None:
    assert _policy_from_text("modelPolicy: SOMETHING_UNDEFINED,", "") is None


def test_wrapped_replaced_and_templated_ids_are_all_stages(tmp_path: Path) -> None:
    root = _engine(tmp_path)
    refs = class_skill_refs(root)
    policies = class_model_policies(root)
    stages = stages_for_workflow(
        root / "agents" / "demo-agent" / "src" / "workflow" / "create-demo-workflow.ts",
        refs,
        policies,
    )
    by_id = {s["id"]: s for s in stages}

    # `rev(att("10-draft-post"))`: the literal through two suffix helpers.
    assert by_id["10-draft-post"]["kind"] == "agent"
    assert by_id["10-draft-post"]["skill_ref"] == "demo-craft@3"
    assert by_id["10-draft-post"]["agent_id"] == "demo-draft"
    assert by_id["10-draft-post"]["default_model"] == "claude-opus-4-8"
    # `stepId.replace("03s-script", "03u-script")`: the replacement is the id in the trace.
    assert by_id["03u-script"]["kind"] == "agent"
    assert by_id["03u-script"]["default_model"] == "gemini-2.5-pro"
    assert by_id["03u-script"]["vendor"] == "gemini"
    # A templated id keeps its literal prefix; a retry template collapses to one stage.
    assert by_id["08-craft-verdict"]["agent_id"] == "demo-judge"
    assert by_id["05-write-copy"]["kind"] == "code"
    assert by_id["11-verify-numbers-sourced"]["kind"] == "code"
    assert by_id["15-batch-review"]["kind"] == "gate"
    # Code steps carry no model facts.
    assert "default_model" not in by_id["00-intake-check"]
