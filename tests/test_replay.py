import json
from pathlib import Path

import pytest

from cua.config import TenantConfig, Workspace
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.secrets import DictSecretStore
from tests.conftest import CREDS
from tests.samples import balance_lookup

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app_profile():
    return Workspace(ROOT / "config").app("cu_teller")


@pytest.fixture
def replay(browser, live_a, app_profile, tmp_path):
    live_a.faults.clear()
    engine = ReplayEngine(browser, DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]}))

    def run(member_number: str, live=live_a, **options):
        tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live.base_url)
        return engine.run(balance_lookup(), tenant, app_profile, {"member_number": member_number},
                          ReplayOptions(evidence_root=tmp_path, **options))

    return run


def evidence_text(result) -> str:
    run_dir = Path(result.evidence_dir)
    return "".join(p.read_text(encoding="utf-8") for p in run_dir.rglob("*") if p.suffix in {".json", ".jsonl"})


def events(result) -> list[dict]:
    lines = (Path(result.evidence_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_success_returns_typed_output_and_redacted_evidence(replay):
    result = replay("100234")
    assert result.status == "success", result.failure
    assert result.outputs == {"savings_balance": {"amount": "4182.55", "currency": "USD"}}
    assert result.app_version == "7.2.4"
    assert set(result.locator_ranks.values()) == {0} and not result.recoveries_applied
    text = evidence_text(result)
    assert "100234" not in text and "4182.55" not in text and "AVERY" not in text
    assert "test-pass" not in text


def test_same_artifact_different_member(replay):
    result = replay("100871")
    assert result.status == "success"
    assert result.outputs["savings_balance"]["amount"] == "12940.00"


def test_member_not_found_is_a_business_outcome(replay):
    result = replay("999999")
    assert result.status == "business_outcome" and result.exit_code == 10
    assert (result.outcome.code, result.outcome.step_id) == ("member_not_found", "s03_submit_search")
    assert result.failure is None and result.outputs == {}


def test_restricted_member_is_access_denied_outcome(replay):
    result = replay("100555")
    assert (result.status, result.outcome.code) == ("business_outcome", "access_denied")


def test_malformed_input_is_rejected_before_touching_the_ui(replay):
    result = replay("12ab")
    assert (result.status, result.failure.category) == ("failure", "invalid_input")
    assert not any(e["type"] == "navigated" for e in events(result))


def test_maintenance_notice_is_dismissed_and_run_continues(replay, live_a):
    live_a.faults.set("maintenance_notice")
    result = replay("100234")
    assert result.status == "success", result.failure
    assert [r.handler for r in result.recoveries_applied] == ["maintenance_notice"]


def test_session_expiry_reauthenticates_and_restarts(replay, live_a):
    live_a.faults.set("session_expired")
    result = replay("100234")
    assert result.status == "success", result.failure
    assert [r.handler for r in result.recoveries_applied] == ["session_expired"]
    assert any(e["type"] == "run_restarted" for e in events(result))


def test_slow_load_is_waited_for_not_failed(replay, live_a):
    live_a.faults.set("slow_load", "3")
    result = replay("100234")
    assert result.status == "success", result.failure


def test_core_error_page_is_a_hard_failure_with_evidence(replay, live_a):
    live_a.faults.set("server_error")
    result = replay("100234", trace=True)
    assert (result.status, result.failure.category) == ("failure", "app_error")
    assert "core_host_unavailable" in result.failure.observed
    run_dir = Path(result.evidence_dir)
    assert (run_dir / result.failure.evidence["screenshot"]).exists()
    assert (run_dir / "trace.zip").exists()


def test_unknown_dialog_escalates_to_a_human(replay, live_a):
    live_a.faults.set("unknown_dialog")
    result = replay("100234")
    assert result.status == "needs_human" and result.exit_code == 20
    request = json.loads((Path(result.evidence_dir) / f"interventions/{result.intervention.intervention_id}.json")
                         .read_text(encoding="utf-8"))
    assert request["reason_code"] == "unknown_dialog" and request["screenshot"]


def test_tenant_variant_without_overlay_fails_clearly(replay, live_b):
    result = replay("100234", live=live_b)
    assert (result.status, result.failure.category) == ("failure", "locator_not_found")
    assert result.failure.step_id == "s01_open_member_inquiry"
    assert result.app_version == "7.3.1"
