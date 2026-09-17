import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cua.config import TenantConfig, Workspace
from cua.discovery.agent import DiscoveryAgent, DiscoveryOptions
from cua.discovery.goal import GoalSpec
from cua.discovery.llm import ModelTurn, ToolCall
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.schema.conditions import FrameUrl
from cua.schema.targets import TableCell
from cua.secrets import DictSecretStore
from tests.conftest import CREDS

ROOT = Path(__file__).resolve().parents[1]
SECRETS = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})


class ScriptedModel:
    """Stands in for the LLM: each scripted move finds its element id in the latest observation text."""

    model_id = "scripted-test-model"

    def __init__(self, moves):
        self.moves = list(moves)
        self.seen: list[str] = []

    def next_turn(self, system, tools, messages):
        text = _texts(messages[-1]["content"])
        self.seen.append(text)
        name, pattern, args = self.moves.pop(0)
        if pattern:
            match = re.search(pattern, text)
            assert match, f"{pattern!r} not in observation:\n{text}"
            args = {"element_id": match.group(1), **args}
        call = ToolCall(id=f"call_{len(self.seen)}", name=name, input=args)
        return ModelTurn(content=[{"type": "tool_use", "id": call.id, "name": name, "input": args}],
                         tool_calls=[call], text="", stop_reason="tool_use", model=self.model_id,
                         input_tokens=100, output_tokens=20)


def _texts(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content:
        if block.get("type") == "text":
            out.append(block["text"])
        elif block.get("type") == "tool_result":
            out.append(_texts(block["content"]))
    return "\n".join(out)


HAPPY_PATH = [
    ("click", r'(e\d+) link "Member Inquiry"', {"intent": "Open Member Inquiry"}),
    ("fill", r'(e\d+) textbox "Member Number"', {"value": {"input": "member_number"}, "intent": "Enter member number"}),
    ("click", r'(e\d+) button "Search"', {"intent": "Search for the member"}),
    ("extract", r'"Regular Savings" \| (e\d+) "', {"output": "savings_balance", "intent": "Read the savings balance"}),
    ("finish", None, {"summary": "done"}),
]

pytestmark = pytest.mark.browser


@pytest.fixture
def setup(browser, live_a, tmp_path):
    live_a.faults.clear()
    workspace = Workspace(ROOT / "config")
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    goal = GoalSpec.load(ROOT / "goals" / "member_savings_balance.yaml")
    options = DiscoveryOptions(evidence_root=tmp_path / "runs", capabilities_root=tmp_path / "capabilities")
    return browser, workspace.app("cu_teller"), tenant, goal, options, tmp_path


def test_discovery_records_a_parameterized_capability_that_replays_for_other_members(setup):
    browser, app, tenant, goal, options, tmp_path = setup
    model = ScriptedModel(HAPPY_PATH)
    result = DiscoveryAgent(browser, model, SECRETS).run(goal, tenant, app, {"member_number": "100234"}, options)
    assert result.status == "recorded", result.reason
    cap = result.capability

    assert [s.action.kind for s in cap.steps] == ["click", "fill", "click", "extract"]
    assert cap.steps[2].post == (FrameUrl(frame_path=("main",), path="/teller/member/{{inputs.member_number}}"),)
    assert isinstance(cap.steps[3].target.locators[0].locator, TableCell)
    assert cap.app.versions == ">=7.2,<8" and cap.status == "draft"
    assert cap.provenance.model_id == "scripted-test-model" and cap.provenance.step_count == 4

    artifact_text = Path(result.capability_path).read_text(encoding="utf-8")
    assert "100234" not in artifact_text and "4,182.55" not in artifact_text
    observations = "\n".join(model.seen)
    assert "100234" not in observations and "AVERY" not in observations

    engine = ReplayEngine(browser, SECRETS)
    replay_options = ReplayOptions(evidence_root=tmp_path / "replays")
    other = engine.run(cap, tenant, app, {"member_number": "100871"}, replay_options)
    assert other.status == "success", other.failure
    assert other.outputs["savings_balance"]["amount"] == "12940.00"
    missing = engine.run(cap, tenant, app, {"member_number": "999999"}, replay_options)
    assert (missing.status, missing.outcome.code) == ("business_outcome", "member_not_found")


def test_literal_input_values_are_refused(setup):
    browser, app, tenant, goal, options, _ = setup
    moves = [
        ("click", r'(e\d+) link "Member Inquiry"', {"intent": "Open Member Inquiry"}),
        ("fill", r'(e\d+) textbox "Member Number"', {"value": {"literal": "100234"}, "intent": "Enter member"}),
        ("request_human", None, {"reason": "stuck"}),
    ]
    model = ScriptedModel(moves)
    result = DiscoveryAgent(browser, model, SECRETS).run(goal, tenant, app, {"member_number": "100234"}, options)
    assert "reference the input instead" in model.seen[2]
    assert result.status == "needs_human"


def test_finish_requires_every_output(setup):
    browser, app, tenant, goal, options, _ = setup
    moves = [
        ("click", r'(e\d+) link "Member Inquiry"', {"intent": "Open Member Inquiry"}),
        ("finish", None, {"summary": "done"}),
        ("request_human", None, {"reason": "cannot find balance"}),
    ]
    model = ScriptedModel(moves)
    result = DiscoveryAgent(browser, model, SECRETS).run(goal, tenant, app, {"member_number": "100234"}, options)
    assert "outputs not extracted" in model.seen[2]
    assert result.status == "needs_human"
    events = [json.loads(line) for line in (Path(result.evidence_dir) / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "intervention_requested" for e in events)


def test_replay_path_never_imports_a_model_client():
    code = ("import sys, cua.replay.engine, cua.cli; "
            "bad = sorted(m for m in sys.modules if m.startswith(('anthropic', 'cua.discovery'))); print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
