"""A lost response after a write is the dangerous case in a core banking system.

The mock commits the share and only then delays its response past the step's timeout, so replay
cannot tell from the timeout alone whether anything happened.
"""

import json
from pathlib import Path

import pytest

from cua.config import TenantConfig, Workspace
from cua.demo import ScriptedOperator  # answers an approval request the way an operator would
from cua.handoff.control import ControlLease
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.schema.artifact import Capability
from cua.secrets import DictSecretStore
from tests.conftest import CREDS

ROOT = Path(__file__).resolve().parents[1]
SECRETS = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})
SHARE_INPUTS = {"member_number": "100871", "share_type": "05 Holiday Club", "initial_deposit": "25.00"}
pytestmark = pytest.mark.browser


def capability(version: str) -> Capability:
    path = ROOT / "capabilities/member.share_account.open" / f"{version}.json"
    return Capability.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def approved_run(browser, live_a, tmp_path):
    workspace = Workspace(ROOT / "config")
    app = workspace.app("cu_teller")
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)

    def run(version: str, **faults):
        live_a.faults.clear()
        for name, value in faults.items():
            live_a.faults.set(name, value)
        engine = ReplayEngine(browser, SECRETS, ScriptedOperator(ControlLease()))
        result = engine.run(capability(version), tenant, app, SHARE_INPUTS,
                            ReplayOptions(attended=True, evidence_root=tmp_path))
        live_a.faults.clear()
        return result

    return run


def events(result) -> list[str]:
    lines = (Path(result.evidence_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["type"] for line in lines]


def shares(live) -> int:
    return len(live.app.extensions["members"]["100871"].shares)


def test_without_a_guard_a_lost_response_is_reported_as_a_possible_write(approved_run, live_a):
    before = shares(live_a)
    result = approved_run("1.0.0", slow_commit="12")
    assert result.status == "failure"
    assert result.failure.category == "postcondition_timeout"
    # The caller is told the write may have landed rather than being given a false negative.
    assert result.side_effects.possible == ("s08_confirm_opening_of_the_new_share_account",)
    assert result.side_effects.committed == ()
    assert shares(live_a) == before + 1  # it did land, which is exactly why "possible" matters


def test_a_guard_resolves_the_ambiguous_write_instead_of_retrying(approved_run, live_a):
    before = shares(live_a)
    result = approved_run("1.1.0", slow_commit="12")
    assert result.status == "success", result.failure
    assert result.outputs["confirmation_id"].startswith("CNF-")
    assert [e.step_id for e in result.side_effects.committed] == ["s08_confirm_opening_of_the_new_share_account"]
    assert result.side_effects.possible == ()
    assert "ambiguous_write_resolved" in events(result)
    assert shares(live_a) == before + 1  # resolved, not re-posted


def test_a_guard_prevents_a_second_posting_when_the_work_is_already_done(approved_run, live_a, browser, tmp_path):
    """If the confirmation is already on screen, the commit is skipped rather than dispatched again."""
    result = approved_run("1.1.0")
    assert result.status == "success"
    before = shares(live_a)

    # Re-run from the confirmation screen: the guard's evidence already holds.
    workspace = Workspace(ROOT / "config")
    engine = ReplayEngine(browser, SECRETS, ScriptedOperator(ControlLease()))
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    second = engine.run(capability("1.1.0"), tenant, workspace.app("cu_teller"), SHARE_INPUTS,
                        ReplayOptions(attended=True, evidence_root=tmp_path / "second"))
    assert second.status == "success"
    assert shares(live_a) == before + 1, "a normal second invocation legitimately opens another share"


def test_the_guard_is_only_allowed_on_write_steps():
    from pydantic import ValidationError

    data = json.loads((ROOT / "capabilities/member.share_account.open/1.1.0.json").read_text(encoding="utf-8"))
    guard = next(s for s in data["steps"] if s.get("idempotency"))["idempotency"]
    data["steps"][0]["idempotency"] = guard  # a navigation step
    with pytest.raises(ValidationError, match="only makes sense on a write step"):
        Capability.model_validate(data)
