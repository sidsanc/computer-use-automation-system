import json
from pathlib import Path

import httpx
import pytest

from cua.config import TenantConfig, Workspace
from cua.discovery.agent import DiscoveryAgent, DiscoveryOptions
from cua.discovery.goal import GoalSpec
from cua.handoff.console import OperatorConsole, SessionOps
from cua.handoff.control import ControlError, ControlLease
from cua.handoff.gate import HumanResolution, InterventionRequest
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.secrets import DictSecretStore
from cua.surface.base import TargetingError
from tests.conftest import CREDS
from tests.samples import balance_lookup
from tests.test_discovery import ScriptedModel

ROOT = Path(__file__).resolve().parents[1]
SECRETS = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})


# ---- control lease (no browser) ------------------------------------------------------------------

def test_lease_enforces_a_single_holder_and_legal_transitions():
    lease = ControlLease()
    assert lease.holder == "automation"
    lease.require("automation")
    with pytest.raises(ControlError, match="held by automation"):
        lease.require("human")

    lease.pause("iv1")
    assert (lease.state, lease.holder, lease.intervention_id) == ("paused", "nobody", "iv1")
    with pytest.raises(ControlError, match="automation tried to act"):
        lease.require("automation")

    lease.take("alex")
    lease.require("human")
    with pytest.raises(ControlError, match="illegal control transition human -> paused"):
        lease.to("paused", "alex")

    lease.hand_back("alex")
    assert lease.state == "resuming" and lease.holder == "nobody"
    lease.to("automation", "automation")
    assert lease.holder == "automation" and lease.intervention_id is None
    assert [(t.frm, t.to) for t in lease.history] == [
        ("automation", "paused"), ("paused", "human"), ("human", "resuming"), ("resuming", "automation")]


def test_lease_reports_failed_resume_back_to_paused():
    lease = ControlLease()
    lease.pause("iv1")
    lease.take("alex")
    lease.hand_back("alex")
    lease.to("paused", "automation", note="resume verification failed")
    assert lease.state == "paused"


# ---- operator console HTTP surface (no browser) --------------------------------------------------

def pending_request() -> InterventionRequest:
    from datetime import UTC, datetime

    return InterventionRequest(
        intervention_id="iv-test", run_id="run1", mode="replay", capability_id="member.savings_balance.lookup",
        step_id="s03_submit_search", reason_code="unknown_dialog", reason="an unmodelled dialog is open",
        allowed=("take_control", "abort"), frame_urls={"main": "http://127.0.0.1/teller"},
        created_at=datetime.now(UTC))


@pytest.fixture
def console():
    console = OperatorConsole(port=8791)
    console.start()
    yield console
    console.stop()


def test_console_requires_the_token_and_relays_decisions(console):
    console.publish(pending_request(), "paused")
    assert httpx.get("http://127.0.0.1:8791/?token=wrong").status_code == 403
    page = httpx.get(console.url)
    assert page.status_code == 200 and "unknown_dialog" in page.text and "take control" in page.text

    assert httpx.get(f"http://127.0.0.1:8791/preview.png?token={console.token}").status_code == 404
    console.set_preview(b"\x89PNG-fake")
    assert httpx.get(f"http://127.0.0.1:8791/preview.png?token={console.token}").content == b"\x89PNG-fake"

    posted = httpx.post(f"http://127.0.0.1:8791/decide?token={console.token}",
                        json={"action": "take_control", "operator": "alex"})
    assert posted.status_code == 200
    decision = console.take_decision()
    assert (decision.kind, decision.operator) == ("take_control", "alex")
    assert console.take_decision() is None  # decisions are consumed once


def test_console_rejects_unknown_actions(console):
    console.publish(pending_request(), "paused")
    assert httpx.post(f"http://127.0.0.1:8791/decide?token={console.token}",
                      json={"action": "rm -rf"}).status_code == 400


# ---- attended replay with a programmatic operator -------------------------------------------------

class ScriptedOperator:
    """Stands in for a person at the console: takes the live session, acts, hands control back."""

    def __init__(self, lease: ControlLease, work=None, operator: str = "alex") -> None:
        self.lease, self.work, self.operator = lease, work, operator
        self.ops: SessionOps | None = None
        self.requests: list[InterventionRequest] = []

    def attach(self, ops: SessionOps, log=None) -> None:
        self.ops = ops

    def request(self, request: InterventionRequest) -> HumanResolution:
        self.requests.append(request)
        self.lease.pause(request.intervention_id)
        self.ops.announce()
        mark = len(self.ops.capture.actions) if self.ops.capture else 0
        self.lease.take(self.operator)
        self.ops.announce()
        if self.work is not None:
            self.work(self.ops.surface)
        actions = self.ops.capture.take_since(mark) if self.ops.capture else ()
        self.lease.hand_back(self.operator)
        return HumanResolution(decision="resumed", operator=self.operator, actions=actions)


def acknowledge_dialog(surface) -> None:
    from cua.schema.targets import RoleName

    frame = surface.page.frame(name="main")
    from cua.surface.web.surface import find

    handles = find(frame, RoleName(role="button", name="Acknowledge"))
    assert len(handles) == 1, "operator could not find the dialog button"
    surface.perform("click", handles[0], actor="human")


pytestmark_browser = pytest.mark.browser


@pytest.mark.browser
def test_operator_takes_the_live_session_and_replay_resumes(browser, live_a, tmp_path):
    live_a.faults.clear()
    live_a.faults.set("unknown_dialog")
    app = Workspace(ROOT / "config").app("cu_teller")
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    lease = ControlLease()
    operator = ScriptedOperator(lease, work=acknowledge_dialog)
    engine = ReplayEngine(browser, SECRETS, operator)

    result = engine.run(balance_lookup(), tenant, app, {"member_number": "100234"},
                        ReplayOptions(attended=True, evidence_root=tmp_path))

    assert result.status == "success", result.failure
    assert result.outputs["savings_balance"]["amount"] == "4182.55"
    assert [r.reason_code for r in operator.requests] == ["unknown_dialog"]
    assert [(t["from"], t["to"]) for t in result.control_transitions] == [
        ("automation", "paused"), ("paused", "human"), ("human", "resuming"), ("resuming", "automation")]
    assert [a.kind for a in result.human_actions] == ["click"]
    assert result.human_actions[0].control.startswith("button")
    assert any(e.actor == "human" for e in result.side_effects.committed)

    events = [json.loads(line) for line in (Path(result.evidence_dir) / "events.jsonl").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert "human_action" in kinds and "resumed_after_human" in kinds
    resumed = next(e for e in events if e["type"] == "resumed_after_human")
    assert resumed["step_id"] == "s04_read_savings_balance"  # the dialog step's postcondition already held


@pytest.mark.browser
def test_automation_cannot_act_while_a_human_holds_control(browser, live_a, tmp_path):
    live_a.faults.clear()
    live_a.faults.set("unknown_dialog")
    app = Workspace(ROOT / "config").app("cu_teller")
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    lease = ControlLease()
    refused: list[str] = []

    def automation_tries_to_act(surface):
        try:
            surface.perform("click", None, actor="automation")
        except ControlError as exc:
            refused.append(str(exc))
        acknowledge_dialog(surface)

    engine = ReplayEngine(browser, SECRETS, ScriptedOperator(lease, work=automation_tries_to_act))
    result = engine.run(balance_lookup(), tenant, app, {"member_number": "100234"},
                        ReplayOptions(attended=True, evidence_root=tmp_path))
    assert result.status == "success", result.failure
    assert refused and "held by human" in refused[0]


@pytest.mark.browser
def test_discovery_records_a_human_step_when_the_operator_takes_over(browser, live_a, tmp_path):
    live_a.faults.clear()
    workspace = Workspace(ROOT / "config")
    app = workspace.app("cu_teller")
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    goal = GoalSpec.load(ROOT / "goals" / "member_savings_balance.yaml")
    moves = [
        ("click", r'(e\d+) link "Member Inquiry"', {"intent": "Open Member Inquiry"}),
        ("request_human", None, {"reason": "the operator must key this part manually"}),
        ("fill", r'(e\d+) textbox "Member Number"', {"value": {"input": "member_number"}, "intent": "Enter member"}),
        ("click", r'(e\d+) button "Search"', {"intent": "Search for the member"}),
        ("extract", r'"Regular Savings" \| (e\d+) "', {"output": "savings_balance", "intent": "Read savings balance"}),
        ("finish", None, {"summary": "done"}),
    ]
    lease = ControlLease()
    agent = DiscoveryAgent(browser, ScriptedModel(moves), SECRETS, ScriptedOperator(lease))
    options = DiscoveryOptions(evidence_root=tmp_path / "runs", capabilities_root=tmp_path / "capabilities")
    result = agent.run(goal, tenant, app, {"member_number": "100234"}, options)

    assert result.status == "recorded", result.reason
    human_steps = [s for s in result.capability.steps if s.performed_by == "human"]
    assert len(human_steps) == 1 and human_steps[0].action.kind == "request_human"
    assert result.capability.requires_human is True


def test_targeting_error_is_not_swallowed_by_operator_helpers():
    assert issubclass(TargetingError, Exception)
