"""Deterministic replay: the production execution path. No model is consulted here.

Per step: settle known states and preconditions → resolve the target (unique match only)
→ policy gate → perform once → wait for the postcondition or a handler, whichever comes first.

Handlers fire in fixed precedence (escalate > hard_failure > business_outcome > recoverable).
Recoveries have budgets and never re-dispatch the step's own action: after dismissing an
interstitial we go back to waiting, because a click that "timed out" may still have landed.
"""

import hashlib
import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from cua.config import AppProfile, TenantConfig
from cua.evidence.log import EvidenceLog
from cua.handoff.capture import HumanCapture
from cua.handoff.console import SessionOps
from cua.handoff.control import ControlLease
from cua.handoff.gate import HumanGate, HumanResolution, InterventionRequest, ReasonCode, UnattendedGate
from cua.policy.gate import ActionContext, Decision, Policy, evaluate, url_allowed
from cua.policy.redact import Redactor
from cua.schema.artifact import Capability, Handler, Step
from cua.schema.base import render_template
from cua.schema.conditions import Condition, DialogPresent, InputRef, LiteralValue, SecretRef
from cua.schema.results import (
    CapabilityRef,
    CommittedEffect,
    Failure,
    InterventionRef,
    InvalidInputError,
    Outcome,
    PolicyDecision,
    RecoveryRecord,
    RunResult,
    SideEffects,
    validate_inputs,
)
from cua.secrets import MissingSecretError, SecretStore
from cua.surface.base import AmbiguousLocatorError, TargetingError
from cua.surface.web.session import WebSession
from cua.surface.web.surface import ActionInfo, render_target
from cua.tenancy.overlay import Overlay
from cua.tenancy.overlay import apply as apply_overlay

PRECEDENCE = {"escalate": 0, "hard_failure": 1, "business_outcome": 2, "recoverable": 3}
POLL_MS = 200


@dataclass
class ReplayOptions:
    attended: bool = False
    allow_irreversible: bool = False
    evidence_root: Path = Path("runs")
    trace: bool = False
    video: bool = False
    success_timeout_s: float = 5
    max_restarts: int = 1


class _Terminal(Exception):
    def __init__(self, status: str, **payload: Any) -> None:
        super().__init__(status)
        self.status, self.payload = status, payload


class _Restart(Exception):
    pass


class _ResumeAt(Exception):
    """Raised after a human hands control back: continue from the step the screen actually supports."""

    def __init__(self, index: int) -> None:
        super().__init__(index)
        self.index = index


@dataclass
class _Run:
    run_id: str
    capability: Capability
    tenant: TenantConfig
    app: AppProfile
    policy: Policy
    options: ReplayOptions
    log: EvidenceLog
    redactor: Redactor
    raw_inputs: dict[str, str]
    started_at: datetime
    t0: float
    inputs: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    session: WebSession | None = None
    app_version: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    ranks: dict[str, int] = field(default_factory=dict)
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    attempts: Counter = field(default_factory=Counter)
    committed: list[CommittedEffect] = field(default_factory=list)
    possible: list[str] = field(default_factory=list)
    lease: ControlLease = field(default_factory=ControlLease)
    capture: HumanCapture | None = None
    current_index: int = 0
    overlay_id: str | None = None

    @property
    def surface(self):
        assert self.session is not None
        return self.session.surface

    def render(self, text: str) -> str:
        return render_template(text, self.inputs)

    def pii_values(self) -> list[str]:
        specs = self.capability.inputs
        return [v for k, v in self.raw_inputs.items() if k in specs and specs[k].sensitivity == "pii"]

    def value_of(self, value: object) -> str:
        match value:
            case InputRef(input=name):
                return self.inputs[name]
            case SecretRef(secret=name):
                return self.secrets[name]
            case LiteralValue(literal=literal):
                return literal
        raise TypeError(f"unsupported value: {value!r}")


def capability_hash(capability: Capability) -> str:
    canonical = json.dumps(capability.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class ReplayEngine:
    def __init__(self, browser: Browser, secrets: SecretStore, gate: HumanGate | None = None) -> None:
        self.browser = browser
        self.secret_store = secrets
        self.gate = gate or UnattendedGate()

    # ---- run lifecycle ---------------------------------------------------------------------------

    def run(
        self,
        capability: Capability,
        tenant: TenantConfig,
        app: AppProfile,
        inputs: dict[str, str],
        options: ReplayOptions | None = None,
        overlay: Overlay | None = None,
    ) -> RunResult:
        options = options or ReplayOptions()
        run_id = uuid.uuid4().hex[:12]
        base_capability = capability
        if overlay is not None:
            capability = apply_overlay(capability, overlay)
        pii = [v for k, v in inputs.items() if k in capability.inputs and capability.inputs[k].sensitivity == "pii"]
        redactor = Redactor(pii_values=pii, sensitive_captions=app.sensitive_captions,
                            sensitive_columns=app.sensitive_columns)
        log = EvidenceLog(options.evidence_root, run_id, "replay", redactor)
        run = _Run(
            run_id=run_id, capability=capability, tenant=tenant, app=app,
            policy=app.policy.for_tenant(tenant.base, tenant.policy), options=options, log=log,
            redactor=redactor, raw_inputs=inputs, started_at=datetime.now(UTC), t0=time.monotonic(),
            lease=getattr(self.gate, "lease", None) or ControlLease(),
            overlay_id=overlay.overlay_id if overlay else None,
        )
        if overlay is not None:
            log.event("overlay_applied", overlay=overlay.overlay_id, base_hash=capability_hash(base_capability),
                      resolved_hash=capability_hash(capability))
        log.event("run_started", capability=self._ref(run), tenant=tenant.tenant_id,
                  inputs=self._display_inputs(run), attended=options.attended,
                  allow_irreversible=options.allow_irreversible)
        result: RunResult | None = None
        try:
            run.inputs = validate_inputs(capability, inputs)
            for name in (*app.login.secrets, *capability.secrets):
                run.secrets[name] = self.secret_store.get(name)
                redactor.add_secret(run.secrets[name])
            video_dir = log.dir / "video" if options.video else None
            run.session = WebSession(self.browser, run.policy.allowed_origins, video_dir=video_dir, trace=options.trace)
            self._wire_handoff(run)
            self._execute(run)
            missing = [name for name in capability.outputs if name not in run.outputs]
            if missing:
                raise _Terminal("failure", failure=self._failure(
                    run, "extraction_failed", None, f"outputs {missing}", "flow finished without extracting them"))
            result = self._result(run, "success", outputs=dict(run.outputs))
        except _Terminal as terminal:
            result = self._result(run, terminal.status, **terminal.payload)
        except InvalidInputError as exc:
            result = self._result(run, "failure", failure=Failure(
                category="invalid_input", step_id=None, expected="inputs matching the capability contract",
                observed=redactor.text(str(exc))))
        except MissingSecretError as exc:
            result = self._result(run, "failure", failure=Failure(
                category="internal_error", step_id=None, expected="configured secrets", observed=str(exc)))
        except PlaywrightError as exc:
            result = self._result(run, "failure", failure=self._failure(
                run, "internal_error", None, "browser automation to keep working", str(exc).splitlines()[0]))
        finally:
            self._close(run, keep_trace=result is None or result.status != "success")
        log.write_json("result.json", self._persistable(result))
        log.event("run_finished", status=result.status, exit_code=result.exit_code, duration_ms=result.duration_ms)
        log.close()
        return result

    def _wire_handoff(self, run: _Run) -> None:
        """Give the session a control lease, and the gate a way to work with it while it waits."""
        run.lease.on_change = lambda t: run.log.event(
            "control_transition", **{"from": t.frm, "to": t.to, "actor": t.actor, "note": t.note})
        run.surface.lease = run.lease
        if not hasattr(self.gate, "attach"):
            return
        run.capture = HumanCapture(run.session.context, run.lease, lambda action, holder: run.log.event(
            "human_action" if holder == "human" else "input_while_automation_held_control",
            kind=action.kind, control=action.control, value=action.value, frame=action.frame_path), run.surface)
        page = run.session.page

        def screenshot() -> bytes | None:
            try:
                png, _ = run.surface.screenshot_masked(list(run.app.sensitive_captions), run.pii_values(),
                                                       columns=list(run.app.sensitive_columns))
                return png
            except PlaywrightError:
                return None

        self.gate.attach(SessionOps(pump=lambda ms: page.wait_for_timeout(ms), screenshot=screenshot,
                                    announce=lambda: run.capture.announce(page), capture=run.capture,
                                    surface=run.surface), run.log)

    def _close(self, run: _Run, keep_trace: bool) -> None:
        if run.session is None:
            return
        if run.session.blocked_requests:
            run.log.event("network_blocked", origins=sorted(set(run.session.blocked_requests)))
        trace_path = run.log.dir / "trace.zip" if run.options.trace and keep_trace else None
        try:
            video = run.session.close(trace_path=trace_path)
        except PlaywrightError:
            return
        if video:
            run.log.event("attachment", name=video.relative_to(run.log.dir).as_posix(),
                          masking="none: video cannot be masked; only fake data is used")

    def _execute(self, run: _Run) -> None:
        restarts = 0
        while True:
            try:
                self._sign_on(run)
                self._goto(run, run.capability.entry.path)
                self._check_version(run)
                steps = run.capability.steps
                run.current_index = 0
                while run.current_index < len(steps):
                    step = steps[run.current_index]
                    try:
                        self._run_step(run, step, self._handlers(run, step))
                    except _ResumeAt as resume:
                        run.current_index = resume.index
                        run.lease.to("automation", "automation", note="resume verified against the live screen")
                        if run.capture is not None:
                            run.capture.announce(run.session.page)
                        run.log.event("resumed_after_human", step_index=resume.index,
                                      step_id=steps[resume.index].id if resume.index < len(steps) else None)
                        continue
                    run.current_index += 1
                self._await(run, None, self._handlers(run, None), run.capability.success,
                            time.monotonic() + run.options.success_timeout_s, phase="success")
                run.log.event("success_verified")
                return
            except _Restart:
                restarts += 1
                if restarts > run.options.max_restarts:
                    raise _Terminal("failure", failure=self._failure(
                        run, "recovery_exhausted", None, f"at most {run.options.max_restarts} restart(s)",
                        "the session had to be restarted again")) from None
                run.outputs.clear()
                run.ranks.clear()
                run.log.event("run_restarted", restart=restarts)

    def _sign_on(self, run: _Run) -> None:
        login = run.app.login
        self._goto(run, login.path)
        for step in login.steps:
            self._run_step(run, step, self._merge(run.app.handlers, step.handlers), record_rank=False)
        self._await(run, None, list(run.app.handlers), login.success, time.monotonic() + 10, phase="success")
        run.log.event("signed_on")

    def _goto(self, run: _Run, path: str) -> None:
        url = run.tenant.base + path
        if not url_allowed(run.policy, url):
            raise _Terminal("policy_blocked", policy=PolicyDecision(
                rule="location_not_allowed", step_id=None, reason=f"entry route {path} is outside the allowlist"))
        run.session.page.goto(url, wait_until="load")
        run.log.event("navigated", path=path)

    def _check_version(self, run: _Run) -> None:
        fp = run.app.fingerprint
        if fp is None:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frames = run.session.page.frames
            for frame in frames:
                try:
                    text = frame.evaluate("() => document.body ? document.body.innerText : ''")
                    match = re.search(fp.text_regex, text)
                except PlaywrightError:
                    continue
                if match:
                    run.app_version = match.group(1)
                    break
            if run.app_version:
                break
            run.session.page.wait_for_timeout(POLL_MS)
        run.log.event("app_fingerprint", version=run.app_version, required=run.capability.app.versions)
        if run.app_version is None:
            return
        try:
            supported = Version(run.app_version) in SpecifierSet(run.capability.app.versions)
        except (InvalidVersion, InvalidSpecifier):
            supported = False
        if not supported:
            raise _Terminal("failure", failure=self._failure(
                run, "unexpected_state", None, f"{run.capability.app.product} {run.capability.app.versions}",
                f"product version {run.app_version}"))

    # ---- steps -------------------------------------------------------------------------------------

    def _run_step(self, run: _Run, step: Step, handlers: list[Handler], record_rank: bool = True) -> None:
        log, surface = run.log, run.surface
        log.event("step_started", step_id=step.id, intent=step.intent, action=step.action.kind)
        self._await(run, step, handlers, step.pre, time.monotonic() + step.timeout_s, phase="pre")

        if step.action.kind == "request_human":
            self._ask_human(run, step, "agent_requested", step.action.reason, ("take_control", "abort"))
            return

        handle, info = None, ActionInfo(location_url=run.session.page.url, control_name=None,
                                        destination_url=None, destination_method=None)
        if step.target is not None:
            handle, rank = self._resolve(run, step, handlers)
            if record_rank:
                run.ranks[step.id] = rank
            key = getattr(step.action, "key", None)
            info = surface.action_info(handle, step.action.kind, key)

        decision = self._gate(run, step, step.action.kind, info)
        writes = decision.risk in ("write", "irreversible")
        if step.action.kind == "wait_for":
            self._await(run, step, handlers, step.post, time.monotonic() + step.timeout_s, phase="post")
            log.event("step_completed", step_id=step.id)
            return
        if decision.risk == "irreversible" and not step.irreversible:
            log.event("risk_upgraded", step_id=step.id, declared=step.effect, policy=decision.risk)

        if writes:
            run.possible.append(step.id)
        value = None
        if step.action.kind == "fill":
            value = run.value_of(step.action.value)
        elif step.action.kind == "select":
            value = run.value_of(step.action.option)
        try:
            text = surface.perform(step.action.kind, handle, value, getattr(step.action, "key", None), step.timeout_s)
        except PlaywrightError as exc:
            self._react(run, step, handlers, phase="act")
            raise _Terminal("failure", failure=self._failure(
                run, "unexpected_state", step.id, f"'{step.action.kind}' to succeed on {step.intent!r}",
                str(exc).splitlines()[0])) from None
        log.event("action_performed", step_id=step.id, action=step.action.kind, risk=decision.risk,
                  locator_rank=run.ranks.get(step.id))

        if step.action.kind == "extract":
            name = step.action.output
            try:
                run.outputs[name] = run.capability.outputs[name].parse(text or "")
            except ValueError as exc:
                raise _Terminal("failure", failure=self._failure(
                    run, "extraction_failed", step.id, f"a {run.capability.outputs[name].type} value",
                    str(exc))) from None
            log.event("output_extracted", step_id=step.id, output=name)

        self._await(run, step, handlers, step.post, time.monotonic() + step.timeout_s, phase="post")
        if writes:
            run.possible.remove(step.id)
            run.committed.append(CommittedEffect(step_id=step.id, actor="automation"))
        log.event("step_completed", step_id=step.id)

    def _gate(self, run: _Run, step: Step | None, kind: str, info: ActionInfo) -> Decision:
        step_id = step.id if step else None
        ctx = ActionContext(
            action=kind, mode="replay", location_url=info.location_url, step_id=step_id,
            control_name=info.control_name, destination_url=info.destination_url,
            destination_method=info.destination_method, attended=run.options.attended,
            capability_approved=run.capability.status == "approved",
            allow_irreversible=run.options.allow_irreversible,
        )
        decision = evaluate(run.policy, ctx)
        run.log.event("policy_decision", step_id=step_id, action=kind, verdict=decision.verdict, risk=decision.risk,
                      rule=decision.rule, reason=decision.reason)
        if decision.verdict == "block":
            raise _Terminal("policy_blocked", policy=PolicyDecision(rule=decision.rule, step_id=step_id,
                                                                    reason=decision.reason))
        if decision.verdict == "require_approval":
            self._ask_human(run, step, "approval_required", decision.reason, ("approve", "deny", "abort"))
        return decision

    def _resolve(self, run: _Run, step: Step, handlers: list[Handler]):
        target = render_target(step.target, run.render)
        deadline = time.monotonic() + step.timeout_s
        last: Exception | None = None
        while True:
            try:
                resolution = run.surface.resolve(target)
                return resolution.handle, resolution.rank
            except AmbiguousLocatorError as exc:
                raise _Terminal("failure", failure=self._failure(
                    run, "ambiguous_locator", step.id, "exactly one matching element", str(exc))) from None
            except (TargetingError, PlaywrightError) as exc:
                last = exc
            if self._react(run, step, handlers, phase="pre"):
                deadline = time.monotonic() + step.timeout_s
                continue
            if time.monotonic() > deadline:
                expected = "; ".join(json.dumps(c.locator.model_dump()) for c in target.locators)
                raise _Terminal("failure", failure=self._failure(
                    run, "locator_not_found", step.id, expected, str(last).splitlines()[0]))
            run.session.page.wait_for_timeout(POLL_MS)

    def _await(self, run: _Run, step: Step | None, handlers: list[Handler], conditions: tuple[Condition, ...],
               deadline: float, phase: str) -> None:
        """Condition-based wait: known states are handled first, then the conditions are checked."""
        while True:
            if self._react(run, step, handlers, phase):
                deadline = max(deadline, time.monotonic() + (step.timeout_s if step else 10))
                continue
            if all(run.surface.check(c, run.render, run.value_of) for c in conditions):
                return
            if time.monotonic() > deadline:
                category = {"pre": "precondition_failed"}.get(phase, "postcondition_timeout")
                expected = "; ".join(json.dumps(c.model_dump(mode="json", exclude_defaults=True)) for c in conditions)
                raise _Terminal("failure", failure=self._failure(
                    run, category, step.id if step else None, run.render(expected), self._observed(run)))
            run.session.page.wait_for_timeout(POLL_MS)

    # ---- handlers ----------------------------------------------------------------------------------

    def _handlers(self, run: _Run, step: Step | None) -> list[Handler]:
        return self._merge(run.app.handlers, run.capability.handlers, step.handlers if step else ())

    @staticmethod
    def _merge(*layers: tuple[Handler, ...]) -> list[Handler]:
        merged: dict[str, Handler] = {}
        for layer in layers:
            for handler in layer:
                merged[handler.code] = handler
        return list(merged.values())

    def _react(self, run: _Run, step: Step | None, handlers: list[Handler], phase: str) -> bool:
        """Detect and handle one known or unknown state. Returns True if something was handled."""
        fired = [h for h in handlers if all(run.surface.check(c, run.render, run.value_of) for c in h.when)]
        if fired:
            self._handle(run, step, min(fired, key=lambda h: PRECEDENCE[h.kind]), phase)
            return True
        known = {c.name for h in handlers for c in h.when if isinstance(c, DialogPresent)}
        unknown = [name for _, name in run.surface.dialogs() if name not in known and None not in known]
        if unknown:
            run.log.event("unknown_dialog", step_id=step.id if step else None, dialogs=unknown)
            self._ask_human(run, step, "unknown_dialog", f"an unmodelled dialog is open: {unknown}",
                            ("take_control", "abort"))
            return True
        return False

    def _handle(self, run: _Run, step: Step | None, handler: Handler, phase: str) -> None:
        step_id = step.id if step else None
        run.log.event("handler_fired", step_id=step_id, code=handler.code, kind=handler.kind, phase=phase)
        match handler.kind:
            case "business_outcome":
                raise _Terminal("business_outcome",
                                outcome=Outcome(code=handler.code, message=handler.message, step_id=step_id))
            case "hard_failure":
                raise _Terminal("failure", failure=self._failure(
                    run, "app_error", step_id, "no application error", f"{handler.code}: {handler.message}"))
            case "escalate":
                self._ask_human(run, step, "escalation_handler", handler.message, ("take_control", "abort"))
                return
        run.attempts[handler.code] += 1
        attempt = run.attempts[handler.code]
        if attempt > handler.max_attempts:
            budget = f"'{handler.code}' resolved within {handler.max_attempts} attempt(s)"
            raise _Terminal("failure", failure=self._failure(run, "recovery_exhausted", step_id, budget,
                                                             handler.message))
        recovery = handler.recovery
        run.recoveries.append(RecoveryRecord(step_id=step_id, handler=handler.code, recovery=recovery.kind,
                                             attempt=attempt))
        if recovery.kind == "dismiss":
            resolution = run.surface.resolve(render_target(recovery.target, run.render))
            info = run.surface.action_info(resolution.handle, "click")
            self._gate(run, step, "click", info)  # recovery actions pass the same policy gate
            run.surface.perform("click", resolution.handle)
        elif recovery.kind == "reauth_and_restart":
            if run.committed or run.possible:
                self._ask_human(run, step, "unsafe_restart",
                                "session expired after a write step was dispatched; restarting could repeat it",
                                ("take_control", "abort"))
                return
            raise _Restart()
        run.log.event("recovery_applied", step_id=step_id, code=handler.code, recovery=recovery.kind, attempt=attempt)

    # ---- humans, evidence and results --------------------------------------------------------------

    def _ask_human(self, run: _Run, step: Step | None, reason_code: ReasonCode, reason: str,
                   allowed: tuple[str, ...]) -> HumanResolution:
        intervention_id = uuid.uuid4().hex[:10]
        screenshot, observation = self._capture(run, f"interventions/{intervention_id}")
        request = InterventionRequest(
            intervention_id=intervention_id, run_id=run.run_id, mode="replay", capability_id=run.capability.id,
            step_id=step.id if step else None, step_intent=step.intent if step else None,
            reason_code=reason_code, reason=reason, allowed=allowed, frame_urls=run.surface.frame_urls(),
            screenshot=screenshot, observation=observation, created_at=datetime.now(UTC),
        )
        run.log.write_json(f"interventions/{intervention_id}.json", request)
        run.log.event("intervention_requested", intervention_id=intervention_id, reason_code=reason_code,
                      step_id=request.step_id)
        resolution = self.gate.request(request)
        run.log.event("intervention_resolved", intervention_id=intervention_id, decision=resolution.decision,
                      operator=resolution.operator, actions=resolution.actions)
        ref = InterventionRef(intervention_id=intervention_id, reason=reason, step_id=request.step_id)
        if resolution.decision == "unavailable":
            raise _Terminal("needs_human", intervention=ref)
        if resolution.decision in ("denied", "aborted"):
            raise _Terminal("policy_blocked", policy=PolicyDecision(
                rule=f"operator_{resolution.decision}", step_id=request.step_id, reason=resolution.note or reason))
        if resolution.decision == "resumed":
            if any(a.kind in ("click", "submit", "press_key") for a in resolution.actions):
                run.committed.append(CommittedEffect(step_id=step.id if step else "(during handoff)", actor="human"))
            raise _ResumeAt(self._resume_index(run))
        return resolution

    def _resume_index(self, run: _Run) -> int:
        """Trust the screen, not the step counter: skip the steps whose postconditions already hold."""
        steps = run.capability.steps
        index = run.current_index
        while index < len(steps):
            post = steps[index].post
            if not post or not all(run.surface.check(c, run.render, run.value_of) for c in post):
                break
            run.log.event("step_satisfied_by_human", step_id=steps[index].id)
            index += 1
        return index

    def _capture(self, run: _Run, prefix: str) -> tuple[str | None, str | None]:
        if run.session is None:
            return None, None
        shot = obs = None
        try:
            png, masked = run.surface.screenshot_masked(list(run.app.sensitive_captions), run.pii_values(),
                                                       columns=list(run.app.sensitive_columns))
            shot = run.log.relative(run.log.attach_masked_bytes(f"{prefix}.png", png, masking=f"blackout:{masked}"))
        except PlaywrightError:
            pass
        try:
            observed = run.redactor.observation(run.surface.observe())
            obs = run.log.relative(run.log.write_json(f"{prefix}-observation.json", observed))
        except (PlaywrightError, TargetingError):
            pass
        return shot, obs

    def _failure(self, run: _Run, category: str, step_id: str | None, expected: str, observed: str) -> Failure:
        screenshot, observation = self._capture(run, "failure")
        evidence = {k: v for k, v in (("screenshot", screenshot), ("observation", observation)) if v}
        if run.options.trace and run.session is not None:
            evidence["trace"] = "trace.zip"
        failure = Failure(category=category, step_id=step_id, expected=run.redactor.text(expected),
                          observed=run.redactor.text(observed), evidence=evidence)
        run.log.event("failure_detected", **failure.model_dump())
        return failure

    def _observed(self, run: _Run) -> str:
        try:
            return json.dumps({"frames": run.surface.frame_urls(), "dialogs": [n for _, n in run.surface.dialogs()],
                               "title": run.session.page.title()})
        except PlaywrightError as exc:
            return str(exc).splitlines()[0]

    @staticmethod
    def _ref(run: _Run) -> CapabilityRef:
        return CapabilityRef(id=run.capability.id, version=run.capability.version,
                             resolved_hash=capability_hash(run.capability), overlay=run.overlay_id)

    @staticmethod
    def _display_inputs(run: _Run) -> dict[str, str]:
        specs = run.capability.inputs
        return {k: "[pii]" if k in specs and specs[k].sensitivity == "pii" else v for k, v in run.raw_inputs.items()}

    def _result(self, run: _Run, status: str, **payload: Any) -> RunResult:
        return RunResult(
            run_id=run.run_id, status=status, capability=self._ref(run), tenant=run.tenant.tenant_id,
            app_version=run.app_version, inputs=self._display_inputs(run),
            recoveries_applied=tuple(run.recoveries), locator_ranks=dict(run.ranks),
            human_actions=tuple(run.capture.actions) if run.capture else (),
            control_transitions=tuple(
                {"at": t.at.isoformat(), "from": t.frm, "to": t.to, "actor": t.actor, "note": t.note or ""}
                for t in run.lease.history),
            side_effects=SideEffects(committed=tuple(run.committed), possible=tuple(run.possible)),
            started_at=run.started_at, duration_ms=int((time.monotonic() - run.t0) * 1000),
            evidence_dir=run.log.dir.as_posix(), **payload,
        )

    @staticmethod
    def _persistable(result: RunResult) -> RunResult:
        """Evidence keeps the shape of outputs but not their values; the caller gets the values."""
        masked = {
            name: {**value, "amount": "[masked]"} if isinstance(value, dict) else "[masked]"
            for name, value in result.outputs.items()
        }
        return result.model_copy(update={"outputs": masked})
