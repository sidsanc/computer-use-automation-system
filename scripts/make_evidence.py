"""Regenerate /evidence: curated replay runs against the mock app, copied with their logs.

Discovery evidence is NOT produced here — it comes from real `cua discover` runs and is
copied in as-is. Everything below is the deterministic path, so it is reproducible:

    uv run python scripts/make_evidence.py

Runs are written to a temporary root and the interesting ones copied into /evidence.
"""

import argparse
import json
import shutil
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.config import TenantConfig, Workspace  # noqa: E402
from cua.handoff.control import ControlLease  # noqa: E402
from cua.handoff.gate import HumanResolution  # noqa: E402
from cua.replay.engine import ReplayEngine, ReplayOptions  # noqa: E402
from cua.schema.artifact import Capability  # noqa: E402
from cua.schema.targets import RoleName  # noqa: E402
from cua.secrets import DictSecretStore  # noqa: E402
from cua.surface.web.surface import find  # noqa: E402
from mockbank.app import create_app  # noqa: E402

CREDS = ("teller01", "evidence-run-password")
EVIDENCE = ROOT / "evidence"
BALANCE = Capability.model_validate_json(
    (ROOT / "capabilities/member.savings_balance.lookup/1.0.0.json").read_text(encoding="utf-8"))
OPEN_SHARE = Capability.model_validate_json(
    (ROOT / "capabilities/member.share_account.open/1.0.0.json").read_text(encoding="utf-8"))


class Mock:
    def __init__(self, variant: str) -> None:
        self.app = create_app(variant, credentials=CREDS)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def faults(self):
        return self.app.extensions["faults"]

    def stop(self) -> None:
        self.server.shutdown()


class ApprovingOperator:
    """Scripted stand-in for a person at the console, so the evidence run is reproducible."""

    def __init__(self, lease: ControlLease, work=None, decision: str = "approved") -> None:
        self.lease, self.work, self.decision = lease, work, decision
        self.ops = None

    def attach(self, ops, log=None) -> None:
        self.ops = ops

    def request(self, request) -> HumanResolution:
        self.lease.pause(request.intervention_id)
        self.ops.announce()
        if self.decision == "approved":
            self.lease.to("automation", "evidence-operator", note="approved")
            return HumanResolution(decision="approved", operator="evidence-operator", note="reviewed: fake data")
        mark = len(self.ops.capture.actions) if self.ops.capture else 0
        self.lease.take("evidence-operator")
        self.ops.announce()
        if self.work:
            self.work(self.ops.surface)
        actions = self.ops.capture.take_since(mark) if self.ops.capture else ()
        self.lease.hand_back("evidence-operator")
        return HumanResolution(decision="resumed", operator="evidence-operator", actions=actions)


def acknowledge_dialog(surface) -> None:
    handles = find(surface.page.frame(name="main"), RoleName(role="button", name="Acknowledge"))
    surface.perform("click", handles[0], actor="human")


def copy_run(result, name: str, note: str) -> dict:
    target = EVIDENCE / name
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(result.evidence_dir, target)
    return {"run": name, "status": result.status, "exit_code": result.exit_code, "note": note,
            "outcome": result.outcome.code if result.outcome else None,
            "failure": result.failure.category if result.failure else None,
            "policy_rule": result.policy.rule if result.policy else None,
            "recoveries": [r.handler for r in result.recoveries_applied],
            "human_actions": len(result.human_actions), "tenant": result.tenant,
            "overlay": result.capability.overlay}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", default=str(ROOT / "runs" / "evidence"))
    args = parser.parse_args()
    runs_root = Path(args.runs_root)
    shutil.rmtree(runs_root, ignore_errors=True)

    workspace = Workspace(ROOT / "config")
    app = workspace.app("cu_teller")
    overlay = workspace.overlay("cu_beta")
    secrets = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})
    alpha_mock, beta_mock = Mock("a"), Mock("b")
    alpha = TenantConfig(tenant_id="cu_alpha", display_name="Alpha Community Credit Union",
                         app="cu_teller", base_url=alpha_mock.url)
    beta = TenantConfig(tenant_id="cu_beta", display_name="Riverbend Federal Credit Union",
                        app="cu_teller", base_url=beta_mock.url)
    index = []

    def options(**kw) -> ReplayOptions:
        return ReplayOptions(evidence_root=runs_root, **kw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        engine = ReplayEngine(browser, secrets)

        alpha_mock.faults.clear()
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100871"}, options()),
            "replay_success_other_member",
            "Replayed for a different member than the one it was recorded with: no run value is baked in."))

        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "999999"}, options()),
            "replay_business_not_found", "'No such member' is an answer for the caller, not a crash."))

        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100555"}, options()),
            "replay_business_access_denied", "Restricted record: a business outcome, not a failure."))

        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "12ab"}, options()),
            "replay_invalid_input", "Caller contract violation, caught before the UI is touched."))

        alpha_mock.faults.set("maintenance_notice")
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100234"}, options()),
            "replay_recovered_interstitial", "Known interstitial dismissed within budget; the run still succeeds."))

        alpha_mock.faults.set("session_expired")
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100234"}, options()),
            "replay_recovered_session_expiry",
            "Re-authenticated and restarted; only safe because nothing had been written yet."))

        alpha_mock.faults.set("slow_load", "4")
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100234"}, options()),
            "replay_slow_load", "A slow screen is waited for by condition, not treated as a failure."))
        alpha_mock.faults.clear()

        alpha_mock.faults.set("server_error")
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100234"}, options(trace=True)),
            "replay_hard_failure_app_error", "Core error page: hard failure with screenshot, observation and trace."))
        alpha_mock.faults.clear()

        alpha_mock.faults.set("unknown_dialog")
        index.append(copy_run(
            engine.run(BALANCE, alpha, app, {"member_number": "100234"}, options()),
            "replay_needs_human_unknown_dialog",
            "An unmodelled dialog escalates; unattended, the run returns needs_human with the request."))

        alpha_mock.faults.set("unknown_dialog")
        lease = ControlLease()
        attended = ReplayEngine(browser, secrets, ApprovingOperator(lease, acknowledge_dialog, "resumed"))
        index.append(copy_run(
            attended.run(BALANCE, alpha, app, {"member_number": "100234"}, options(attended=True)),
            "handoff_operator_takes_control",
            "Operator takes the live session, clears the dialog, hands back; replay resumes from the verified state."))
        alpha_mock.faults.clear()

        index.append(copy_run(
            engine.run(OPEN_SHARE, alpha, app,
                       {"member_number": "100871", "share_type": "05 Holiday Club", "initial_deposit": "25.00"},
                       options()),
            "replay_policy_blocked_irreversible",
            "Unattended replay refuses the irreversible commit: draft capability, and the caller did not opt in."))

        lease = ControlLease()
        approving = ReplayEngine(browser, secrets, ApprovingOperator(lease, decision="approved"))
        index.append(copy_run(
            approving.run(OPEN_SHARE, alpha, app,
                          {"member_number": "100871", "share_type": "05 Holiday Club", "initial_deposit": "25.00"},
                          options(attended=True)),
            "replay_irreversible_after_approval",
            "With a human approving in an attended run, the same capability commits and returns the confirmation id."))

        index.append(copy_run(
            engine.run(BALANCE, beta, app, {"member_number": "100234"}, options()),
            "tenant_beta_without_overlay", "Second tenant, no overlay: fails loudly on the reworded control."))

        index.append(copy_run(
            engine.run(BALANCE, beta, app, {"member_number": "100234"}, options(), overlay=overlay),
            "tenant_beta_with_overlay", "Same artifact plus the tenant overlay: succeeds on the rebranded app."))

        browser.close()
    alpha_mock.stop()
    beta_mock.stop()

    (EVIDENCE / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    for row in index:
        print(f"{row['run']:<40} {row['status']:<16} exit={row['exit_code']}")


if __name__ == "__main__":
    main()
