"""The guided tour: every interesting replay behaviour, back to back, against the mock app.

`cua demo` narrates these for a reviewer; scripts/make_evidence.py runs the same list and copies
the runs into /evidence. Both use one definition so the tour and the evidence cannot drift apart.

No model is involved: this is the deterministic path end to end.
"""

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser

from cua.config import AppProfile, TenantConfig, Workspace
from cua.handoff.control import ControlLease
from cua.handoff.gate import HumanResolution
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.schema.artifact import Capability
from cua.schema.results import RunResult
from cua.schema.targets import RoleName
from cua.secrets import DictSecretStore
from cua.surface.web.surface import find
from cua.tenancy.overlay import Overlay

DEMO_CREDENTIALS = ("teller01", "demo-run-password")
SHARE_INPUTS = {"member_number": "100871", "share_type": "05 Holiday Club", "initial_deposit": "25.00"}


class MockApp:
    """The legacy target app, served in-process on an ephemeral port."""

    def __init__(self, variant: str) -> None:
        from werkzeug.serving import make_server

        from mockbank.app import create_app

        self.app = create_app(variant, credentials=DEMO_CREDENTIALS)
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def faults(self):
        return self.app.extensions["faults"]

    def stop(self) -> None:
        self._server.shutdown()


class ScriptedOperator:
    """Stands in for a person at the console so the tour is reproducible.

    `approve` answers an approval request; `take_over` takes the live session, does the manual
    work and hands control back — the same code path the real operator console drives.
    """

    def __init__(self, lease: ControlLease, work: Callable | None = None, decision: str = "approved") -> None:
        self.lease, self.work, self.decision = lease, work, decision
        self.ops = None

    def attach(self, ops, log=None) -> None:
        self.ops = ops

    def request(self, request) -> HumanResolution:
        self.lease.pause(request.intervention_id)
        self.ops.announce()
        if self.decision == "approved":
            self.lease.to("automation", "demo-operator", note="approved after review")
            return HumanResolution(decision="approved", operator="demo-operator", note="reviewed: fictional data")
        mark = len(self.ops.capture.actions) if self.ops.capture else 0
        self.lease.take("demo-operator")
        self.ops.announce()
        if self.work is not None:
            self.work(self.ops.surface)
        actions = self.ops.capture.take_since(mark) if self.ops.capture else ()
        self.lease.hand_back("demo-operator")
        return HumanResolution(decision="resumed", operator="demo-operator", actions=actions)


def acknowledge_dialog(surface) -> None:
    handles = find(surface.page.frame(name="main"), RoleName(role="button", name="Acknowledge"))
    if handles:
        surface.perform("click", handles[0], actor="human")


@dataclass
class Tour:
    """Everything a scenario may use."""

    browser: Browser
    app: AppProfile
    overlay: Overlay
    alpha: TenantConfig
    beta: TenantConfig
    alpha_mock: MockApp
    beta_mock: MockApp
    balance: Capability
    open_share: Capability
    open_share_guarded: Capability
    runs_root: Path
    video_for: frozenset[str] = frozenset()
    secrets: DictSecretStore = field(
        default_factory=lambda: DictSecretStore(
            {"teller_username": DEMO_CREDENTIALS[0], "teller_password": DEMO_CREDENTIALS[1]}))

    def options(self, name: str, **kw) -> ReplayOptions:
        return ReplayOptions(evidence_root=self.runs_root, video=name in self.video_for, **kw)

    def replay(self, name: str, capability: Capability, tenant: TenantConfig, inputs: dict[str, str],
               operator: ScriptedOperator | None = None, overlay: Overlay | None = None, **kw) -> RunResult:
        engine = ReplayEngine(self.browser, self.secrets, operator)
        return engine.run(capability, tenant, self.app, inputs, self.options(name, **kw), overlay=overlay)


@dataclass
class Scenario:
    name: str
    headline: str
    note: str
    run: Callable[[Tour], RunResult]


def _handoff(tour: Tour) -> RunResult:
    tour.alpha_mock.faults.set("unknown_dialog")
    lease = ControlLease()
    operator = ScriptedOperator(lease, acknowledge_dialog, decision="resumed")
    result = tour.replay("handoff_operator_takes_control", tour.balance, tour.alpha,
                         {"member_number": "100234"}, operator=operator, attended=True)
    tour.alpha_mock.faults.clear()
    return result


def _ambiguous_write(guarded: bool) -> Callable[[Tour], RunResult]:
    name = f"replay_ambiguous_write_{'resolved' if guarded else 'unguarded'}"

    def run(tour: Tour) -> RunResult:
        tour.alpha_mock.faults.set("slow_commit", "12")
        capability = tour.open_share_guarded if guarded else tour.open_share
        result = tour.replay(name, capability, tour.alpha, SHARE_INPUTS,
                             operator=ScriptedOperator(ControlLease()), attended=True)
        tour.alpha_mock.faults.clear()
        return result

    return run


def _with_fault(name: str, fault: str, value: str | None = None, **kw) -> Callable[[Tour], RunResult]:
    def run(tour: Tour) -> RunResult:
        tour.alpha_mock.faults.set(fault, value)
        result = tour.replay(name, tour.balance, tour.alpha, {"member_number": "100234"}, **kw)
        tour.alpha_mock.faults.clear()
        return result

    return run


def scenarios() -> list[Scenario]:
    return [
        Scenario("replay_success_other_member", "Replay for a different member than the recording",
                 "No value from the discovery run is baked into the artifact; every locator matched first choice.",
                 lambda t: t.replay("replay_success_other_member", t.balance, t.alpha, {"member_number": "100871"})),
        Scenario("replay_business_not_found", "'No such member' is an answer, not a crash",
                 "A business outcome with a code the caller handles, and exit code 10.",
                 lambda t: t.replay("replay_business_not_found", t.balance, t.alpha, {"member_number": "999999"})),
        Scenario("replay_business_access_denied", "A restricted record is also a business outcome",
                 "Permission denial is reported to the caller, not raised as a failure.",
                 lambda t: t.replay("replay_business_access_denied", t.balance, t.alpha, {"member_number": "100555"})),
        Scenario("replay_invalid_input", "A malformed input never reaches the UI",
                 "The caller broke the contract, so the browser is never opened.",
                 lambda t: t.replay("replay_invalid_input", t.balance, t.alpha, {"member_number": "12ab"})),
        Scenario("replay_recovered_interstitial", "A known interstitial is dismissed and the run continues",
                 "Recovery is reported alongside the status, never mistaken for the answer.",
                 _with_fault("replay_recovered_interstitial", "maintenance_notice")),
        Scenario("replay_recovered_session_expiry", "Session expiry re-authenticates and restarts",
                 "Allowed only because nothing had been written yet.",
                 _with_fault("replay_recovered_session_expiry", "session_expired")),
        Scenario("replay_slow_load", "A slow screen is waited for, not failed",
                 "Waits are condition-based, so a four-second load is simply 'not ready yet'.",
                 _with_fault("replay_slow_load", "slow_load", "4")),
        Scenario("replay_hard_failure_app_error", "A core error page stops the run with evidence",
                 "Step, expected vs observed, masked screenshot, redacted observation and a trace.",
                 _with_fault("replay_hard_failure_app_error", "server_error", trace=True)),
        Scenario("replay_needs_human_unknown_dialog", "An unmodelled dialog escalates instead of guessing",
                 "Unattended, the run returns needs_human with the intervention request saved.",
                 _with_fault("replay_needs_human_unknown_dialog", "unknown_dialog")),
        Scenario("handoff_operator_takes_control", "A person takes the live session and hands it back",
                 "Control transitions are recorded, the human's actions captured, and replay resumes "
                 "from the step the screen actually supports.", _handoff),
        Scenario("replay_policy_blocked_irreversible", "An irreversible commit is refused unattended",
                 "Draft capability and no caller opt-in, so policy blocks it with exit code 30.",
                 lambda t: t.replay("replay_policy_blocked_irreversible", t.open_share, t.alpha, SHARE_INPUTS)),
        Scenario("replay_irreversible_after_approval", "The same commit proceeds once a human approves",
                 "Attended run, operator approves, the confirmation number comes back as a typed output.",
                 lambda t: t.replay("replay_irreversible_after_approval", t.open_share, t.alpha, SHARE_INPUTS,
                                    operator=ScriptedOperator(ControlLease()), attended=True)),
        Scenario("replay_ambiguous_write_unguarded", "A lost response after a commit, with no duplicate guard",
                 "The write did land. Replay cannot prove it, so it fails and reports the write as "
                 "possible rather than claiming nothing happened.", _ambiguous_write(guarded=False)),
        Scenario("replay_ambiguous_write_resolved", "The same lost response, with a duplicate guard",
                 "The step declares evidence that the work landed, so replay reconciles instead of "
                 "retrying: one posting, confirmation returned.", _ambiguous_write(guarded=True)),
        Scenario("tenant_beta_without_overlay", "A second tenant, reworded, without its overlay",
                 "The brittle fallback matches the wrong control (rank 1) and the step's own check catches it.",
                 lambda t: t.replay("tenant_beta_without_overlay", t.balance, t.beta, {"member_number": "100234"})),
        Scenario("tenant_beta_with_overlay", "The same artifact plus a tenant overlay",
                 "Wording differences only; the contract and the risk are untouched.",
                 lambda t: t.replay("tenant_beta_with_overlay", t.balance, t.beta, {"member_number": "100234"},
                                    overlay=t.overlay)),
    ]


def run_tour(browser: Browser, config_dir: Path, capabilities_dir: Path, runs_root: Path,
             video_for: frozenset[str] = frozenset()) -> Iterator[tuple[Scenario, RunResult]]:
    """Run every scenario, yielding each result as it completes."""
    workspace = Workspace(config_dir)
    alpha_mock, beta_mock = MockApp("a"), MockApp("b")
    load = lambda name: Capability.model_validate_json(  # noqa: E731
        (capabilities_dir / name).read_text(encoding="utf-8"))
    tour = Tour(
        browser=browser, app=workspace.app("cu_teller"), overlay=workspace.overlay("cu_beta"),
        alpha=TenantConfig(tenant_id="cu_alpha", display_name="Alpha Community Credit Union",
                           app="cu_teller", base_url=alpha_mock.url),
        beta=TenantConfig(tenant_id="cu_beta", display_name="Riverbend Federal Credit Union",
                          app="cu_teller", base_url=beta_mock.url),
        alpha_mock=alpha_mock, beta_mock=beta_mock,
        balance=load("member.savings_balance.lookup/1.0.0.json"),
        open_share=load("member.share_account.open/1.0.0.json"),
        open_share_guarded=load("member.share_account.open/1.1.0.json"),
        runs_root=runs_root, video_for=video_for,
    )
    try:
        for scenario in scenarios():
            alpha_mock.faults.clear()
            yield scenario, scenario.run(tour)
    finally:
        alpha_mock.stop()
        beta_mock.stop()
