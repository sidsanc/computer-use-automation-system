"""The real runners, driven over an in-memory surface.

If these pass, the Surface and Session ports are real: the replay engine executes a
capability end to end — policy gate, handlers, outcomes, duplicate guards — with no browser,
no network and no Playwright import. That is the same seam a desktop (UIA/AX) surface
would plug into.
"""

import sys
from datetime import UTC, datetime

import pytest

from cua.config import AppProfile, TenantConfig
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.schema.artifact import Capability, Entry
from cua.secrets import DictSecretStore
from cua.surface.fake import Control, FakeApp, FakeSessions, Move, Screen

BASE = "https://fake.app"


def teller_app() -> FakeApp:
    """A three-screen teller: look a member up, read a balance, or post a transfer."""
    return FakeApp(
        base_url=BASE,
        start="home",
        screens=[
            Screen("home", "/home", [Control("link", "Member Inquiry")]),
            Screen("inquiry", "/inquiry", [
                Control("textbox", "", label="Member Number"),
                Control("button", "Search"),
            ]),
            Screen("member", "/member/100871", [
                Control("cell", "S01", column="Share ID", row_key=("Share ID", "S01")),
                Control("cell", "$12,940.00", column="Balance", row_key=("Share ID", "S01")),
                Control("button", "Transfer Funds"),
            ], text="SHARES"),
            Screen("not_found", "/inquiry", [Control("button", "Search")],
                   text="ERR-404 MEMBER NOT ON FILE"),
            Screen("posted", "/transfer/done", [Control("cell", "CNF-900100")],
                   text="TRANSFER POSTED"),
        ],
        moves=[
            Move("home", "Member Inquiry", "inquiry"),
            Move("inquiry", "Search", "member"),
            Move("member", "Transfer Funds", "posted", writes=True, irreversible=True),
        ],
    )


def lookup_capability() -> Capability:
    box = {"frame_path": ["main"], "locators": [
        {"locator": {"kind": "label_neighbor", "role": "textbox", "label": "Member Number"},
         "rationale": "caption beside the field"}]}
    return Capability.model_validate({
        "id": "member.balance.lookup", "version": "1.0.0", "title": "Read a balance",
        "description": "Look a member up and read their share balance.",
        "app": {"product": "fake_teller", "versions": ">=1,<2"},
        "entry": {"path": "/home"},
        "inputs": {"member_number": {"type": "string", "description": "member number",
                                     "pattern": r"^\d{6}$", "sensitivity": "pii"}},
        "outputs": {"balance": {"type": "money", "description": "share balance", "currency": "USD"}},
        "steps": [
            {"id": "s01_open_inquiry", "intent": "Open Member Inquiry", "action": {"kind": "click"},
             "effect": "navigation", "target": {"frame_path": ["main"], "locators": [
                 {"locator": {"kind": "role_name", "role": "link", "name": "Member Inquiry"},
                  "rationale": "menu link"}]},
             "post": [{"kind": "frame_url", "frame_path": ["main"], "path": "/inquiry"}]},
            {"id": "s02_enter_number", "intent": "Enter the member number",
             "action": {"kind": "fill", "value": {"input": "member_number"}}, "target": box, "effect": "none"},
            {"id": "s03_search", "intent": "Search", "action": {"kind": "click"}, "effect": "navigation",
             "target": {"frame_path": ["main"], "locators": [
                 {"locator": {"kind": "role_name", "role": "button", "name": "Search"}, "rationale": "button"}]},
             "post": [{"kind": "text_visible", "text": "SHARES", "frame_path": ["main"]}],
             "handlers": [{"code": "member_not_found", "kind": "business_outcome",
                           "message": "No member with that number.",
                           "when": [{"kind": "text_visible", "text": "ERR-404 MEMBER NOT ON FILE"}]}]},
            {"id": "s04_read_balance", "intent": "Read the share balance",
             "action": {"kind": "extract", "output": "balance"}, "effect": "none",
             "target": {"frame_path": ["main"], "locators": [
                 {"locator": {"kind": "table_cell", "column": "Balance",
                              "row": {"column": "Share ID", "equals": "S01"}}, "rationale": "column + row key"}]}},
        ],
        "success": [{"kind": "text_visible", "text": "SHARES", "frame_path": ["main"]}],
        "provenance": {"method": "hand_authored", "recorded_at": datetime.now(UTC).isoformat(),
                       "recorder_version": "test"},
    })


def transfer_capability() -> Capability:
    return Capability.model_validate({
        "id": "member.funds.transfer", "version": "1.0.0", "title": "Post a transfer",
        "description": "Post a transfer and read the confirmation.",
        "app": {"product": "fake_teller", "versions": ">=1,<2"},
        "entry": {"path": "/member/100871"},
        "inputs": {}, "outputs": {},
        "steps": [
            {"id": "s01_post_transfer", "intent": "Post the transfer to the core",
             "action": {"kind": "click"}, "effect": "write", "irreversible": True,
             "target": {"frame_path": ["main"], "locators": [
                 {"locator": {"kind": "role_name", "role": "button", "name": "Transfer Funds"},
                  "rationale": "commit button"}]},
             "post": [{"kind": "text_visible", "text": "TRANSFER POSTED", "frame_path": ["main"]}],
             "idempotency": {"evidence": [{"kind": "text_visible", "text": "TRANSFER POSTED"}]}},
        ],
        "success": [{"kind": "text_visible", "text": "TRANSFER POSTED", "frame_path": ["main"]}],
        "provenance": {"method": "hand_authored", "recorded_at": datetime.now(UTC).isoformat(),
                       "recorder_version": "test"},
    })


def profile() -> AppProfile:
    return AppProfile.model_validate({
        "product": "fake_teller", "name": "Fake Teller",
        "policy": {"allowed_paths": ["/**"], "allowed_actions": ["click", "fill", "extract", "wait_for"],
                   "irreversible_paths": ["/transfer/**"]},
        "login": {"path": "/home", "secrets": [], "steps": [
            {"id": "s01_noop", "intent": "Already signed on", "action": {"kind": "wait_for"},
             "effect": "none", "post": [{"kind": "frame_url", "frame_path": ["main"], "path": "/**"}]}],
            "success": [{"kind": "frame_url", "frame_path": ["main"], "path": "/**"}]},
    })


@pytest.fixture
def run(tmp_path):
    def go(capability: Capability, app: FakeApp, **options):
        tenant = TenantConfig(tenant_id="fake", display_name="Fake CU", app="fake_teller", base_url=BASE)
        engine = ReplayEngine(FakeSessions(app), DictSecretStore({}))
        inputs = {"member_number": "100871"} if capability.inputs else {}
        return engine.run(capability, tenant, profile(), inputs,
                          ReplayOptions(evidence_root=tmp_path, **options))

    return go


def test_the_engine_runs_a_capability_with_no_browser(run):
    app = teller_app()
    result = run(lookup_capability(), app)
    assert result.status == "success", result.failure
    assert result.outputs["balance"] == {"amount": "12940.00", "currency": "USD"}
    assert app.history[-3:] == ["home", "inquiry", "member"]
    assert set(result.locator_ranks.values()) == {0}


def test_a_handler_still_turns_an_error_screen_into_a_business_outcome(run):
    app = teller_app()
    app.moves = [m for m in app.moves if not (m.screen == "inquiry" and m.control == "Search")]
    app.moves.append(Move("inquiry", "Search", "not_found"))
    result = run(lookup_capability(), app)
    assert (result.status, result.outcome.code) == ("business_outcome", "member_not_found")
    assert result.exit_code == 10 and result.outputs == {}


def test_policy_still_blocks_an_irreversible_write_unattended(run):
    app = teller_app()
    result = run(transfer_capability(), app)
    assert (result.status, result.policy.rule) == ("policy_blocked", "irreversible_unattended")
    assert app.writes == [], "nothing may post when policy refuses"


def test_a_duplicate_guard_skips_a_write_that_already_happened(run):
    """A run resumed after a crash finds the confirmation already on screen."""
    app = teller_app()
    capability = transfer_capability().model_copy(update={"entry": Entry(path="/transfer/done")})
    result = run(capability, app, attended=True, allow_irreversible=True)
    assert result.status == "success", result.failure
    assert app.writes == [], "the guard must stop a second posting"
    assert [e.step_id for e in result.side_effects.committed] == ["s01_post_transfer"]


def test_these_runs_never_import_playwright():
    """The port is only real if the engine can work without the web implementation."""
    import subprocess

    code = ("import cua.replay.engine, cua.surface.fake, sys; "
            "print(sorted(m for m in sys.modules if m.startswith(('playwright','cua.surface.web'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out
