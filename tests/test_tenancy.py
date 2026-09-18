import json
from pathlib import Path

import pytest

from cua.config import TenantConfig, Workspace
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.schema.targets import LabelNeighbor, RoleName
from cua.secrets import DictSecretStore
from cua.tenancy.overlay import Overlay, OverlayError, apply
from tests.conftest import CREDS
from tests.samples import balance_lookup

ROOT = Path(__file__).resolve().parents[1]
SECRETS = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})


@pytest.fixture(scope="module")
def beta_overlay():
    return Workspace(ROOT / "config").overlay("cu_beta")


def test_overlay_prepends_tenant_wording_without_losing_the_original(beta_overlay):
    resolved = apply(balance_lookup(), beta_overlay)
    nav, member_box = resolved.steps[0].target.locators, resolved.steps[1].target.locators
    assert nav[0].locator == RoleName(role="link", name="Account Inquiry")
    assert nav[1].locator == RoleName(role="link", name="Member Inquiry")  # base tenant still works
    assert member_box[0].locator == LabelNeighbor(role="textbox", label="Account No.")
    assert "overlay 'cu_beta'" in nav[0].rationale


def test_overlay_cannot_change_the_contract_or_risk(beta_overlay):
    base = balance_lookup()
    resolved = apply(base, beta_overlay)
    assert resolved.inputs == base.inputs and resolved.outputs == base.outputs
    assert [s.id for s in resolved.steps] == [s.id for s in base.steps]
    assert [(s.effect, s.irreversible) for s in resolved.steps] == [(s.effect, s.irreversible) for s in base.steps]
    assert [s.action for s in resolved.steps] == [s.action for s in base.steps]


def test_overlay_is_checked_against_product_and_steps():
    base = balance_lookup()
    with pytest.raises(OverlayError, match="is for product"):
        apply(base, Overlay(overlay_id="x", product="other_core"))
    with pytest.raises(OverlayError, match="unknown steps"):
        apply(base, Overlay(overlay_id="x", product="cu_teller", steps={"s99_nope": {}}))
    with pytest.raises(OverlayError, match="does not apply"):
        apply(base, Overlay(overlay_id="x", product="cu_teller", applies_to=("other.capability",)))


@pytest.mark.browser
def test_one_capability_serves_both_tenants(browser, live_a, live_b, beta_overlay, tmp_path):
    live_a.faults.clear()
    live_b.faults.clear()
    app = Workspace(ROOT / "config").app("cu_teller")
    capability = balance_lookup()
    engine = ReplayEngine(browser, SECRETS)
    options = ReplayOptions(evidence_root=tmp_path)

    alpha = TenantConfig(tenant_id="cu_alpha", display_name="Alpha", app="cu_teller", base_url=live_a.base_url)
    beta = TenantConfig(tenant_id="cu_beta", display_name="Riverbend", app="cu_teller", base_url=live_b.base_url)

    on_alpha = engine.run(capability, alpha, app, {"member_number": "100234"}, options)
    assert on_alpha.status == "success" and on_alpha.capability.overlay is None

    without_overlay = engine.run(capability, beta, app, {"member_number": "100234"}, options)
    assert (without_overlay.status, without_overlay.failure.category) == ("failure", "locator_not_found")

    with_overlay = engine.run(capability, beta, app, {"member_number": "100234"}, options, overlay=beta_overlay)
    assert with_overlay.status == "success", with_overlay.failure
    assert with_overlay.outputs["savings_balance"]["amount"] == "4182.55"
    assert with_overlay.capability.overlay == "cu_beta"
    assert with_overlay.app_version == "7.3.1"
    assert with_overlay.capability.resolved_hash != on_alpha.capability.resolved_hash
    events = [json.loads(line) for line in (Path(with_overlay.evidence_dir) / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "overlay_applied" for e in events)
