"""LLM-driven discovery: observe → decide → act on a live surface, recording as it goes.

For every action the model picks an element id; before acting, the surface derives and
verifies ranked locators for that element, and after acting it derives a postcondition from
what changed. The model's transcript is evidence, not the artifact.
"""

import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from cua.actions import ACTION_SPECS
from cua.config import AppProfile, TenantConfig
from cua.discovery.goal import GoalSpec
from cua.discovery.llm import ModelClient, ToolCall
from cua.discovery.recorder import RecordedAction, RecordingError, RunFacts, build_capability, next_version, save
from cua.evidence.log import EvidenceLog
from cua.handoff.capture import HumanCapture
from cua.handoff.console import SessionOps
from cua.handoff.control import ControlLease
from cua.handoff.gate import HumanGate, InterventionRequest, ReasonCode, UnattendedGate
from cua.policy.gate import ActionContext, Decision, Policy, evaluate, url_allowed
from cua.policy.redact import Redactor
from cua.schema.artifact import Capability
from cua.schema.base import render_template
from cua.schema.conditions import Condition, ElementPresent, FrameUrl, InputRef, LiteralValue, SecretRef, Value
from cua.schema.results import InvalidInputError, check_inputs
from cua.secrets import MissingSecretError, SecretStore
from cua.surface.base import CONTAINER_ROLES, INTERACTIVE_ROLES, Observation, TargetingError
from cua.surface.web.session import WebSession
from cua.surface.web.surface import render_target

PRECEDENCE = {"escalate": 0, "hard_failure": 1, "business_outcome": 2, "recoverable": 3}

SYSTEM_PROMPT = """\
You are the discovery agent in a computer-use automation system for credit-union back-office \
applications. You operate a live legacy application to accomplish one goal. Your successful run is \
recorded as a deterministic capability that later replays without you, for other inputs.

How you see the application:
- Each turn you receive an accessibility view of every frame (element ids such as e12, roles, names) \
and a screenshot. Personal data is masked.
- Element ids are valid only for the latest observation.

How you act:
- Take exactly one action per turn with the tools.
- Type caller inputs by reference, e.g. {"input": "member_number"}. You never see input values and \
must not type them as literals. Use {"literal": "..."} only for fixed choices that are the same for \
every invocation.
- Read each declared output with `extract` on the element that displays it.
- Give every action a short `intent` a reviewer can understand; it becomes the step description. \
Never put input or output values in an intent.
- Every action you take becomes a recorded step, so take the direct path an experienced operator \
would take and avoid exploratory clicks.
- Actions pass a policy check. If one is blocked, choose another path or call request_human. \
Irreversible actions pause for a human's approval.
- Known interstitials are handled for you; you will be told when that happens.
- If the application shows a state you cannot resolve, or you are unsure how to proceed safely, call \
request_human with the reason instead of guessing.
- When every output has been extracted and the goal is met, call finish."""


@dataclass
class DiscoveryOptions:
    evidence_root: Path = Path("runs")
    capabilities_root: Path = Path("capabilities")
    timeout_s: float = 300
    no_progress_limit: int = 3
    error_limit: int = 3
    video: bool = False


@dataclass
class DiscoveryResult:
    run_id: str
    status: str  # recorded | needs_human | failed | policy_blocked
    reason: str
    evidence_dir: str
    turns: int
    capability_path: str | None = None
    capability: Capability | None = None
    intervention_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class _Stop(Exception):
    def __init__(self, status: str, reason: str, intervention_id: str | None = None) -> None:
        super().__init__(reason)
        self.status, self.reason, self.intervention_id = status, reason, intervention_id


@dataclass
class _Ctx:
    run_id: str
    goal: GoalSpec
    tenant: TenantConfig
    app: AppProfile
    policy: Policy
    inputs: dict[str, str]
    secrets: dict[str, str]
    log: EvidenceLog
    redactor: Redactor
    options: DiscoveryOptions
    session: WebSession | None = None
    app_version: str | None = None
    obs: Observation | None = None
    actions: list[RecordedAction] = field(default_factory=list)
    extracted: dict[str, str] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0})
    served_models: set[str] = field(default_factory=set)
    turns: int = 0
    lease: ControlLease = field(default_factory=ControlLease)
    capture: HumanCapture | None = None

    @property
    def surface(self):
        return self.session.surface

    def render(self, text: str) -> str:
        return render_template(text, self.inputs)

    def pii_values(self) -> list[str]:
        return [v for k, v in self.inputs.items() if self.goal.inputs[k].sensitivity == "pii"]

    def value_of(self, value: object) -> str:
        match value:
            case InputRef(input=name):
                return self.inputs[name]
            case SecretRef(secret=name):
                return self.secrets[name]
            case LiteralValue(literal=literal):
                return literal
        raise TypeError(value)


def tool_definitions(goal: GoalSpec) -> list[dict[str, Any]]:
    element = {"type": "string", "description": "Element id from the latest observation, e.g. e12."}
    intent = {"type": "string", "description": "Short description of this step for a reviewer, without data values."}
    value = {
        "type": "object",
        "description": '{"input": "<name>"} for a caller input, or {"literal": "<text>"} for a fixed choice.',
        "properties": {"input": {"type": "string", "enum": sorted(goal.inputs) or [""]}, "literal": {"type": "string"}},
        "additionalProperties": False,
    }

    def tool(name: str, properties: dict, required: list[str]) -> dict:
        return {"name": name, "description": ACTION_SPECS[name].description,
                "input_schema": {"type": "object", "properties": properties, "required": required,
                                 "additionalProperties": False}}

    return [
        tool("click", {"element_id": element, "intent": intent}, ["element_id", "intent"]),
        tool("fill", {"element_id": element, "value": value, "intent": intent}, ["element_id", "value", "intent"]),
        tool("select", {"element_id": element, "option": value, "intent": intent}, ["element_id", "option", "intent"]),
        tool("press_key", {"element_id": element, "key": {"type": "string", "enum": ["Enter", "Tab", "Escape"]},
                           "intent": intent}, ["element_id", "key", "intent"]),
        tool("extract", {"element_id": element, "output": {"type": "string", "enum": sorted(goal.outputs) or [""]},
                         "intent": intent}, ["element_id", "output", "intent"]),
        tool("wait_for", {"reason": {"type": "string"}}, ["reason"]),
        tool("request_human", {"reason": {"type": "string"}}, ["reason"]),
        tool("finish", {"summary": {"type": "string"}}, ["summary"]),
    ]


def prompt_template_hash(tools: list[dict]) -> str:
    return hashlib.sha256((SYSTEM_PROMPT + json.dumps(tools, sort_keys=True)).encode()).hexdigest()[:16]


class DiscoveryAgent:
    def __init__(self, browser: Browser, model: ModelClient, secrets: SecretStore, gate: HumanGate | None = None):
        self.browser = browser
        self.model = model
        self.secret_store = secrets
        self.gate = gate or UnattendedGate()

    def run(self, goal: GoalSpec, tenant: TenantConfig, app: AppProfile, inputs: dict[str, str],
            options: DiscoveryOptions | None = None) -> DiscoveryResult:
        options = options or DiscoveryOptions()
        run_id = uuid.uuid4().hex[:12]
        pii = [v for k, v in inputs.items() if k in goal.inputs and goal.inputs[k].sensitivity == "pii"]
        redactor = Redactor(pii_values=pii, sensitive_captions=app.sensitive_captions,
                            sensitive_columns=app.sensitive_columns)
        log = EvidenceLog(options.evidence_root, run_id, "discovery", redactor)
        ctx = _Ctx(run_id=run_id, goal=goal, tenant=tenant, app=app,
                   policy=app.policy.for_tenant(tenant.base, tenant.policy), inputs={}, secrets={}, log=log,
                   redactor=redactor, options=options, lease=getattr(self.gate, "lease", None) or ControlLease())
        tools = tool_definitions(goal)
        log.event("discovery_started", goal=goal.goal, capability_id=goal.capability_id, tenant=tenant.tenant_id,
                  model=self.model.model_id, prompt_template_hash=prompt_template_hash(tools),
                  inputs={k: "[pii]" if k in pii else v for k, v in inputs.items()})
        result: DiscoveryResult
        try:
            ctx.inputs = check_inputs(goal.inputs, inputs)
            for name in app.login.secrets:
                ctx.secrets[name] = self.secret_store.get(name)
                redactor.add_secret(ctx.secrets[name])
            ctx.session = WebSession(self.browser, ctx.policy.allowed_origins,
                                     video_dir=log.dir / "video" if options.video else None)
            self._wire_handoff(ctx)
            self._sign_on(ctx)
            entry_frames = self._enter(ctx)
            self._loop(ctx, tools)
            capability, path = self._record(ctx, tools, entry_frames)
            result = DiscoveryResult(run_id, "recorded", f"saved {capability.id}@{capability.version}",
                                     log.dir.as_posix(), ctx.turns, path.as_posix(), capability)
        except _Stop as stop:
            result = DiscoveryResult(run_id, stop.status, stop.reason, log.dir.as_posix(), ctx.turns,
                                     intervention_id=stop.intervention_id)
        except (InvalidInputError, MissingSecretError, RecordingError) as exc:
            result = DiscoveryResult(run_id, "failed", redactor.text(str(exc)), log.dir.as_posix(), ctx.turns)
        except PlaywrightError as exc:
            log.event("browser_error", detail=str(exc).splitlines()[0])
            result = DiscoveryResult(run_id, "failed", f"browser error: {str(exc).splitlines()[0]}",
                                     log.dir.as_posix(), ctx.turns)
        finally:
            if ctx.session is not None:
                try:
                    video = ctx.session.close()
                    if video:
                        log.event("attachment", name=video.relative_to(log.dir).as_posix(),
                                  masking="none: video cannot be masked; only fake data is used")
                except PlaywrightError:
                    pass
        result.usage = dict(ctx.usage)
        log.event("discovery_finished", status=result.status, reason=result.reason, turns=ctx.turns,
                  capability=result.capability_path, usage=ctx.usage, served_models=sorted(ctx.served_models))
        log.close()
        return result

    # ---- session setup ------------------------------------------------------------------------------

    def _wire_handoff(self, ctx: _Ctx) -> None:
        ctx.lease.on_change = lambda t: ctx.log.event(
            "control_transition", **{"from": t.frm, "to": t.to, "actor": t.actor, "note": t.note})
        ctx.surface.lease = ctx.lease
        if not hasattr(self.gate, "attach"):
            return
        ctx.capture = HumanCapture(ctx.session.context, ctx.lease, lambda action, holder: ctx.log.event(
            "human_action" if holder == "human" else "input_while_automation_held_control",
            kind=action.kind, control=action.control, value=action.value, frame=action.frame_path), ctx.surface)
        page = ctx.session.page

        def screenshot() -> bytes | None:
            try:
                png, _ = ctx.surface.screenshot_masked(list(ctx.app.sensitive_captions), ctx.pii_values(),
                                                       columns=list(ctx.app.sensitive_columns))
                return png
            except PlaywrightError:
                return None

        self.gate.attach(SessionOps(pump=lambda ms: page.wait_for_timeout(ms), screenshot=screenshot,
                                    announce=lambda: ctx.capture.announce(page), capture=ctx.capture,
                                    surface=ctx.surface), ctx.log)

    def _sign_on(self, ctx: _Ctx) -> None:
        login = ctx.app.login
        self._goto(ctx, login.path)
        for step in login.steps:
            handle = self._resolve_waiting(ctx, step.target, step.timeout_s)
            info = ctx.surface.action_info(handle, step.action.kind)
            self._decide(ctx, step.action.kind, info, step.id, intent=step.intent)
            value = ctx.value_of(step.action.value) if step.action.kind == "fill" else None
            ctx.surface.perform(step.action.kind, handle, value)
        if not self._wait_conditions(ctx, login.success, 10):
            raise _Stop("failed", "sign-on did not reach the expected page")
        ctx.log.event("signed_on")

    def _enter(self, ctx: _Ctx) -> dict[str, str]:
        self._goto(ctx, ctx.goal.entry)
        ctx.surface.settle()
        page_text = " ".join(self._frame_texts(ctx))
        fingerprint = ctx.app.fingerprint
        if fingerprint:
            match = re.search(fingerprint.text_regex, page_text)
            ctx.app_version = match.group(1) if match else None
        ctx.log.event("app_fingerprint", version=ctx.app_version)
        return ctx.surface.frame_urls()

    def _goto(self, ctx: _Ctx, path: str) -> None:
        url = ctx.tenant.base + path
        if not url_allowed(ctx.policy, url):
            raise _Stop("policy_blocked", f"route {path} is outside the allowlist")
        ctx.session.page.goto(url, wait_until="load")
        ctx.log.event("navigated", path=path)

    def _frame_texts(self, ctx: _Ctx) -> list[str]:
        texts = []
        for frame in ctx.session.page.frames:
            try:
                texts.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except PlaywrightError:
                continue
        return texts

    # ---- the loop ------------------------------------------------------------------------------------

    def _loop(self, ctx: _Ctx, tools: list[dict]) -> int:
        started = time.monotonic()
        notes = self._auto_recover(ctx)
        first = self._observation_content(ctx, 0, notes)
        contract = {
            "inputs": {k: v.description for k, v in ctx.goal.inputs.items()},
            "outputs": {k: f"{v.type}: {v.description}" for k, v in ctx.goal.outputs.items()},
        }
        intro = (f"Goal: {ctx.goal.goal}\n\nContract (inputs are available by reference only):\n"
                 f"{json.dumps(contract, indent=2)}\n\nCurrent state follows.")
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": intro}, *first]}]
        no_progress = errors = 0
        max_turns = ctx.policy.max_steps
        for turn in range(1, max_turns + 1):
            ctx.turns = turn
            if time.monotonic() - started > ctx.options.timeout_s:
                raise _Stop("failed", f"discovery timed out after {ctx.options.timeout_s:.0f}s")
            reply = self.model.next_turn(SYSTEM_PROMPT, tools, messages)
            ctx.usage["input"] += reply.input_tokens
            ctx.usage["output"] += reply.output_tokens
            ctx.usage["cache_read"] += reply.cache_read_tokens
            ctx.served_models.add(reply.model)
            call = reply.tool_calls[0] if reply.tool_calls else None
            ctx.log.event("model_turn", turn=turn, model=reply.model, stop_reason=reply.stop_reason,
                          text=reply.text, tool=call.name if call else None, tool_input=call.input if call else None,
                          input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                          cache_read_tokens=reply.cache_read_tokens)
            messages.append({"role": "assistant", "content": reply.content})

            if reply.stop_reason == "refusal":
                raise _Stop("failed", "the model declined the request")
            if call is None:
                messages.append({"role": "user", "content": "Take the next action with a tool, or call finish."})
                no_progress += 1
                if no_progress >= ctx.options.no_progress_limit:
                    self._ask_human(ctx, "no_progress", "the agent stopped taking actions")
                continue

            before = ctx.obs.digest() if ctx.obs else None
            outcome, is_error, done = self._execute(ctx, call)
            ctx.log.event("tool_result", turn=turn, tool=call.name, is_error=is_error, result=outcome)
            if done:
                return turn
            notes = self._auto_recover(ctx)
            content = [{"type": "text", "text": outcome}, *self._observation_content(ctx, turn, notes)]
            if is_error:  # the API requires error tool results to be text-only
                content = [block for block in content if block["type"] == "text"]
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error}]})

            errors = errors + 1 if is_error else 0
            changed = ctx.obs.digest() != before
            no_progress = 0 if changed or call.name in ("extract", "wait_for") else no_progress + 1
            if errors >= ctx.options.error_limit:
                self._ask_human(ctx, "action_errors", f"{errors} consecutive actions failed")
                errors = 0
            if no_progress >= ctx.options.no_progress_limit:
                self._ask_human(ctx, "no_progress", f"the page did not change after {no_progress} actions")
                no_progress = 0
        raise _Stop("failed", f"goal not reached within {max_turns} turns")

    def _observation_content(self, ctx: _Ctx, turn: int, notes: list[str]) -> list[dict]:
        ctx.surface.settle()
        ctx.obs = ctx.surface.observe()
        ctx.log.event("observed", turn=turn, frames=["/".join(f.path) or "top" for f in ctx.obs.frames],
                      unavailable=list(ctx.obs.unavailable_frames), nodes=len(ctx.obs.nodes))
        view = ctx.redactor.observation(ctx.obs).render()
        image, masked = ctx.surface.screenshot_masked(list(ctx.app.sensitive_captions), ctx.pii_values(),
                                                      columns=list(ctx.app.sensitive_columns), jpeg=True)
        ctx.log.attach_masked_bytes(f"steps/{turn:02d}.jpg", image, masking=f"blackout:{masked}")
        text = "\n".join([*notes, view]) if notes else view
        return [
            {"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image).decode()}},
        ]

    # ---- actions -------------------------------------------------------------------------------------

    def _execute(self, ctx: _Ctx, call: ToolCall) -> tuple[str, bool, bool]:
        name, args = call.name, call.input
        if name == "finish":
            missing = [o for o in ctx.goal.outputs if o not in ctx.extracted]
            if missing:
                return f"Cannot finish yet: outputs not extracted: {missing}.", True, False
            if not ctx.actions:
                return "Cannot finish: no steps were recorded.", True, False
            return "Finished.", False, True
        if name == "request_human":
            self._ask_human(ctx, "agent_requested", str(args.get("reason", "")))
            return "The operator handled the situation; continue from the current state.", False, False
        if name == "wait_for":
            ctx.surface.settle(timeout_s=10)
            return "Waited for the page to settle.", False, False
        if name not in ("click", "fill", "select", "press_key", "extract"):
            return f"Unknown tool {name!r}.", True, False

        eid, intent = str(args.get("element_id", "")), str(args.get("intent", "")).strip()
        if not intent:
            return "Every action needs an intent.", True, False
        if ctx.redactor.text(intent) != intent or any(v and v in intent for v in ctx.extracted.values()):
            return "The intent must not contain data values; describe the step instead.", True, False
        try:
            node = ctx.obs.node(eid)
            target = ctx.surface.target_for(eid)
            handle = ctx.surface.element(eid)
        except TargetingError as exc:
            return f"Cannot use element {eid!r}: {exc}", True, False

        value: Value | None = None
        if name in ("fill", "select"):
            raw = args.get("value" if name == "fill" else "option") or {}
            if raw.get("input"):
                if raw["input"] not in ctx.goal.inputs:
                    return f"Unknown input {raw['input']!r}.", True, False
                value = InputRef(input=raw["input"])
            elif "literal" in raw:
                if any(raw["literal"] == v for v in ctx.inputs.values()):
                    return "That literal equals a caller input; reference the input instead.", True, False
                value = LiteralValue(literal=str(raw["literal"]))
            else:
                return 'Provide {"input": name} or {"literal": text}.', True, False
        if name == "extract" and args.get("output") not in ctx.goal.outputs:
            return f"Unknown output {args.get('output')!r}.", True, False

        key = args.get("key") if name == "press_key" else None
        info = ctx.surface.action_info(handle, name, key)
        decision = self._decide(ctx, name, info, None, intent=intent, soft=True)
        if decision is None:
            return "Blocked by policy for this deployment; choose another way or request a human.", True, False

        frames_before = ctx.surface.frame_urls()
        pre_keys = {(n.frame_path, n.role, n.name) for n in ctx.obs.nodes}
        try:
            # Re-resolve through the derived locators: an approval or takeover can take minutes,
            # and this also proves at record time that the locators we are about to store work.
            fresh = ctx.surface.resolve(target)
            if fresh.rank > 0:
                ctx.log.event("locator_fallback_at_record_time", intent=intent, rank=fresh.rank)
            text = ctx.surface.perform(name, fresh.handle, ctx.value_of(value) if value else None, key)
        except TargetingError as exc:
            return f"The element could not be found again before acting: {exc}", True, False
        except PlaywrightError as exc:
            return f"The action failed: {str(exc).splitlines()[0]}", True, False

        recorded = RecordedAction(kind=name, intent=intent, target=target, risk=decision.risk, value=value, key=key,
                                  output=args.get("output") if name == "extract" else None)
        if name == "extract":
            spec = ctx.goal.outputs[args["output"]]
            try:
                parsed = spec.parse(text or "")
            except ValueError as exc:
                return f"That element does not contain a {spec.type} value: {exc}", True, False
            ctx.extracted[args["output"]] = (text or "").strip()
            ctx.actions.append(recorded)
            ctx.log.event("step_recorded", kind=name, intent=intent, node_role=node.role, locators=len(target.locators))
            shown = parsed if spec.sensitivity == "public" else f"a {spec.type} value (masked in logs)"
            return f"Extracted {args['output']}: {json.dumps(shown)}", False, False

        ctx.surface.settle()
        recorded.post = self._infer_post(ctx, frames_before, pre_keys) if decision.risk != "read" else ()
        ctx.actions.append(recorded)
        ctx.log.event("step_recorded", kind=name, intent=intent, risk=decision.risk, node_role=node.role,
                      locators=len(target.locators), postconditions=len(recorded.post))
        return f"Performed {name}.", False, False

    def _decide(self, ctx: _Ctx, kind: str, info, step_id: str | None, intent: str,
                soft: bool = False) -> Decision | None:
        decision = evaluate(ctx.policy, ActionContext(
            action=kind, mode="discovery", location_url=info.location_url, step_id=step_id,
            control_name=info.control_name, destination_url=info.destination_url,
            destination_method=info.destination_method))
        ctx.log.event("policy_decision", action=kind, intent=intent, verdict=decision.verdict, risk=decision.risk,
                      rule=decision.rule, reason=decision.reason)
        if decision.verdict == "block":
            if soft:
                return None
            raise _Stop("policy_blocked", decision.reason)
        if decision.verdict == "require_approval":
            self._ask_human(ctx, "approval_required", f"{intent}: {decision.reason}", approval=True)
        return decision

    def _infer_post(self, ctx: _Ctx, frames_before: dict[str, str], pre_keys: set) -> tuple[Condition, ...]:
        conditions: list[Condition] = []
        for label, url in ctx.surface.frame_urls().items():
            if frames_before.get(label) != url:
                path = () if label == "top" else tuple(label.split("/"))
                conditions.append(FrameUrl(frame_path=path, path=self._canonical_path(ctx, url)))
        if conditions:
            return tuple(conditions)
        after = ctx.surface.observe()
        run_values = self._run_values(ctx)
        for node in after.nodes:
            fresh = (node.frame_path, node.role, node.name) not in pre_keys
            if (node.eid and fresh and node.role in INTERACTIVE_ROLES | CONTAINER_ROLES | {"heading"}
                    and not any(v in node.name for v in run_values)):
                try:
                    return (ElementPresent(target=ctx.surface.target_for(node.eid)),)
                except TargetingError:
                    continue
        return ()

    def _canonical_path(self, ctx: _Ctx, url: str) -> str:
        segments = urlsplit(url).path.split("/")
        by_value = {v: k for k, v in ctx.inputs.items() if v}
        return "/".join(f"{{{{inputs.{by_value[s]}}}}}" if s in by_value else s for s in segments) or "/"

    def _run_values(self, ctx: _Ctx) -> list[str]:
        values = [v for v in ctx.inputs.values() if v]
        for text in ctx.extracted.values():
            values.append(text)
            values.append(text.replace("$", "").replace(",", ""))
        return [v for v in values if len(v) >= 3]

    # ---- known states and humans -------------------------------------------------------------------

    def _auto_recover(self, ctx: _Ctx) -> list[str]:
        notes: list[str] = []
        for _ in range(3):
            ctx.surface.settle()
            fired = [h for h in ctx.app.handlers if all(ctx.surface.check(c, ctx.render, ctx.value_of) for c in h.when)]
            if not fired:
                break
            handler = min(fired, key=lambda h: PRECEDENCE[h.kind])
            if handler.kind == "recoverable" and handler.recovery.kind == "dismiss":
                try:
                    handle = ctx.surface.resolve(render_target(handler.recovery.target, ctx.render)).handle
                    info = ctx.surface.action_info(handle, "click")
                    self._decide(ctx, "click", info, None, intent=f"Dismiss {handler.code}")
                    ctx.surface.perform("click", handle)
                except (TargetingError, PlaywrightError):
                    break
                ctx.log.event("recovery_applied", code=handler.code)
                notes.append(f"[system] Known state '{handler.code}' was handled automatically.")
                continue
            ctx.log.event("known_state_detected", code=handler.code, kind=handler.kind)
            notes.append(f"[system] Known application state detected: {handler.code} ({handler.kind}): "
                         f"{handler.message}")
            break
        return notes

    def _ask_human(self, ctx: _Ctx, reason_code: ReasonCode, reason: str, approval: bool = False) -> None:
        intervention_id = uuid.uuid4().hex[:10]
        shot = None
        try:
            png, masked = ctx.surface.screenshot_masked(list(ctx.app.sensitive_captions), ctx.pii_values(),
                                                       columns=list(ctx.app.sensitive_columns))
            shot = ctx.log.relative(ctx.log.attach_masked_bytes(f"interventions/{intervention_id}.png", png,
                                                                masking=f"blackout:{masked}"))
        except PlaywrightError:
            pass
        request = InterventionRequest(
            intervention_id=intervention_id, run_id=ctx.run_id, mode="discovery", capability_id=ctx.goal.capability_id,
            goal=ctx.goal.goal, step_id=None, step_intent=None, reason_code=reason_code, reason=reason,
            allowed=("approve", "deny", "abort") if approval else ("take_control", "abort"),
            frame_urls=ctx.surface.frame_urls(), screenshot=shot, created_at=datetime.now(UTC),
        )
        ctx.log.write_json(f"interventions/{intervention_id}.json", request)
        ctx.log.event("intervention_requested", intervention_id=intervention_id, reason_code=reason_code)
        resolution = self.gate.request(request)
        ctx.log.event("intervention_resolved", intervention_id=intervention_id, decision=resolution.decision,
                      operator=resolution.operator, actions=resolution.actions)
        if resolution.decision == "unavailable":
            raise _Stop("needs_human", reason, intervention_id)
        if resolution.decision in ("denied", "aborted"):
            detail = f"operator {resolution.decision}: {resolution.note or reason}"
            raise _Stop("policy_blocked", detail, intervention_id)
        if resolution.decision == "resumed":
            # What the operator did by hand becomes a step the capability declares a human performs.
            summary = ", ".join(f"{a.kind} {a.control}" for a in resolution.actions) or reason
            ctx.actions.append(RecordedAction(
                kind="request_human", intent=f"Operator performs this part manually: {reason}", target=None,
                risk="read", performed_by="human"))
            ctx.log.event("human_step_recorded", reason=reason, actions=summary)
            ctx.lease.to("automation", "automation", note="discovery resumed")
            if ctx.capture is not None:
                ctx.capture.announce(ctx.session.page)

    # ---- recording -----------------------------------------------------------------------------------

    def _record(self, ctx: _Ctx, tools: list[dict], entry_frames: dict[str, str]) -> tuple[Capability, Path]:
        final = []
        for label, url in ctx.surface.frame_urls().items():
            if entry_frames.get(label) != url:
                path = () if label == "top" else tuple(label.split("/"))
                final.append(FrameUrl(frame_path=path, path=self._canonical_path(ctx, url)))
        facts = RunFacts(run_id=ctx.run_id, model_id=",".join(sorted(ctx.served_models)) or self.model.model_id,
                         prompt_template_hash=prompt_template_hash(tools), input_tokens=ctx.usage["input"],
                         output_tokens=ctx.usage["output"], run_values=self._run_values(ctx))
        version = next_version(ctx.options.capabilities_root / ctx.goal.capability_id)
        capability = build_capability(ctx.goal, ctx.app.product, ctx.app_version, ctx.actions, tuple(final),
                                      facts, version)
        path = save(capability, ctx.options.capabilities_root)
        ctx.log.write_json("capability.json", capability)
        ctx.log.event("capability_recorded", id=capability.id, version=version, path=path.as_posix(),
                      steps=len(capability.steps))
        return capability, path

    def _resolve_waiting(self, ctx: _Ctx, target, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return ctx.surface.resolve(render_target(target, ctx.render)).handle
            except (TargetingError, PlaywrightError) as exc:
                if time.monotonic() > deadline:
                    raise _Stop("failed", f"sign-on control not found: {exc}") from None
            ctx.session.page.wait_for_timeout(200)

    def _wait_conditions(self, ctx: _Ctx, conditions, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if all(ctx.surface.check(c, ctx.render, ctx.value_of) for c in conditions):
                return True
            ctx.session.page.wait_for_timeout(200)
        return False
