"""A minimal operator surface plus the gate that blocks the run while a human works.

Deliberately small: one local page with the intervention context, a live-ish preview and
four buttons. It never touches Playwright (browser objects belong to the run's thread) —
the run pushes fresh masked previews while it waits.
"""

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from cua.evidence.log import EvidenceLog
from cua.handoff.capture import HumanCapture
from cua.handoff.control import ControlLease
from cua.handoff.gate import HumanResolution, InterventionRequest

DecisionKind = Literal["take_control", "hand_back", "approve", "deny", "abort"]


@dataclass
class SessionOps:
    """What the gate may do with the live session while it waits (all on the run's thread)."""

    pump: Callable[[int], None]
    screenshot: Callable[[], bytes | None]
    announce: Callable[[], None]
    capture: HumanCapture | None = None
    surface: object | None = None  # for programmatic operators (test doubles, scripted runbooks)


@dataclass
class Decision:
    kind: DecisionKind
    operator: str
    note: str | None = None


class OperatorConsole:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(12)
        self._lock = threading.Lock()
        self._pending: InterventionRequest | None = None
        self._state = "idle"
        self._preview: bytes | None = None
        self._decision: Decision | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.app = self._build_app()

    # ---- run-thread side ---------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def publish(self, request: InterventionRequest | None, state: str) -> None:
        with self._lock:
            self._pending, self._state, self._decision = request, state, None

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def set_preview(self, png: bytes | None) -> None:
        with self._lock:
            self._preview = png

    def take_decision(self) -> Decision | None:
        with self._lock:
            decision, self._decision = self._decision, None
            return decision

    # ---- HTTP side ---------------------------------------------------------------------------

    def _check(self, request: Request) -> None:
        if not secrets.compare_digest(request.query_params.get("token", ""), self.token):
            raise HTTPException(status_code=403, detail="bad or missing token")

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="cua operator console")

        @app.get("/", response_class=HTMLResponse)
        def index(request: Request) -> HTMLResponse:
            self._check(request)
            with self._lock:
                return HTMLResponse(_page(self._pending, self._state, self.token))

        @app.get("/state")
        def state(request: Request) -> JSONResponse:
            self._check(request)
            with self._lock:
                pending = self._pending.model_dump(mode="json") if self._pending else None
                return JSONResponse({"state": self._state, "intervention": pending})

        @app.get("/preview.png")
        def preview(request: Request) -> Response:
            self._check(request)
            with self._lock:
                if self._preview is None:
                    raise HTTPException(status_code=404, detail="no preview yet")
                return Response(self._preview, media_type="image/png")

        @app.post("/decide")
        async def decide(request: Request) -> JSONResponse:
            self._check(request)
            body = await request.json()
            kind = body.get("action")
            if kind not in ("take_control", "hand_back", "approve", "deny", "abort"):
                raise HTTPException(status_code=400, detail="unknown action")
            with self._lock:
                if self._pending is None:
                    raise HTTPException(status_code=409, detail="nothing to decide")
                self._decision = Decision(kind=kind, operator=str(body.get("operator") or "operator"),
                                          note=body.get("note"))
            return JSONResponse({"accepted": kind})

        return app


def _page(pending: InterventionRequest | None, state: str, token: str) -> str:
    if pending is None:
        return ("<title>Operator console</title><h3>Operator console</h3>"
                f"<p>State: {escape(state)}. Nothing pending.</p>")
    rows = "".join(
        f"<tr><td><b>{escape(k)}</b></td><td>{escape(str(v))}</td></tr>"
        for k, v in [("capability", pending.capability_id), ("goal", pending.goal), ("step", pending.step_id),
                     ("intent", pending.step_intent), ("why", f"{pending.reason_code}: {pending.reason}"),
                     ("frames", pending.frame_urls), ("control", state)]
        if v
    )
    buttons = "".join(
        f"<button onclick=\"decide('{a}')\">{a.replace('_', ' ')}</button> "
        for a in (*pending.allowed, "hand_back")
    )
    return f"""<title>Operator console</title>
<style>body{{font:13px system-ui;margin:16px;max-width:900px}}td{{padding:2px 8px;vertical-align:top}}
button{{font:13px system-ui;padding:6px 10px;margin-right:6px}}img{{max-width:100%;border:1px solid #ccc}}</style>
<h3>Intervention {escape(pending.intervention_id)}</h3>
<table>{rows}</table>
<p>{buttons}<span id="msg"></span></p>
<img id="shot" src="/preview.png?token={token}">
<script>
const token = {token!r};
async function decide(action) {{
  const operator = window.prompt('Operator name', 'operator') || 'operator';
  const r = await fetch('/decide?token=' + token, {{method: 'POST', headers: {{'content-type': 'application/json'}},
    body: JSON.stringify({{action, operator}})}});
  document.getElementById('msg').textContent = r.ok ? action + ' sent' : 'failed: ' + r.status;
}}
setInterval(() => {{
  document.getElementById('shot').src = '/preview.png?token=' + token + '&t=' + Date.now();
  fetch('/state?token=' + token).then(r => r.json()).then(s => {{ document.title = 'Operator · ' + s.state; }});
}}, 2000);
</script>"""


class AttendedGate:
    """Pauses the run, offers the live session to a human, and resumes when control comes back."""

    def __init__(self, console: OperatorConsole, lease: ControlLease, *, timeout_s: float = 300,
                 log: EvidenceLog | None = None, preview_every_s: float = 2.0) -> None:
        self.console, self.lease = console, lease
        self.timeout_s, self.preview_every_s = timeout_s, preview_every_s
        self.log = log
        self.ops: SessionOps | None = None

    def attach(self, ops: SessionOps, log: EvidenceLog | None = None) -> None:
        self.ops = ops
        self.log = log or self.log

    def request(self, request: InterventionRequest) -> HumanResolution:
        if self.ops is None:
            raise RuntimeError("AttendedGate.attach() must be called with the live session first")
        self.lease.pause(request.intervention_id)
        self.ops.announce()
        self.console.publish(request, self.lease.state)
        self._note("operator_console_waiting", url=self.console.url, intervention_id=request.intervention_id)

        decision = self._wait_for(("take_control", "approve", "deny", "abort"))
        if decision is None:
            self.lease.to("automation", "system", note="operator timed out")
            self.ops.announce()
            return HumanResolution(decision="unavailable", note="no operator responded")
        if decision.kind in ("approve", "deny", "abort"):
            self.lease.to("automation", decision.operator, note=decision.kind)
            self.ops.announce()
            mapped = {"approve": "approved", "deny": "denied", "abort": "aborted"}[decision.kind]
            return HumanResolution(decision=mapped, operator=decision.operator, note=decision.note)

        # Hand the live session over, then wait for it back.
        capture = self.ops.capture
        mark = len(capture.actions) if capture else 0
        self.lease.take(decision.operator)
        self.ops.announce()
        self.console.set_state(self.lease.state)
        self._note("control_transferred", to="human", operator=decision.operator,
                   intervention_id=request.intervention_id)

        back = self._wait_for(("hand_back", "abort"))
        actions = capture.take_since(mark) if capture else ()
        if back is None or back.kind == "abort":
            self.lease.to("closed", (back.operator if back else "system"), note="aborted during human control")
            self.ops.announce()
            return HumanResolution(decision="aborted", operator=back.operator if back else None,
                                   note="operator aborted or did not hand control back", actions=actions)
        self.lease.hand_back(back.operator)
        self.ops.announce()
        self._note("control_returned", operator=back.operator, human_actions=len(actions))
        return HumanResolution(decision="resumed", operator=back.operator, note=back.note, actions=actions)

    def _wait_for(self, kinds: tuple[str, ...]) -> Decision | None:
        deadline = time.monotonic() + self.timeout_s
        next_preview = 0.0
        while time.monotonic() < deadline:
            decision = self.console.take_decision()
            if decision is not None and decision.kind in kinds:
                return decision
            if time.monotonic() >= next_preview:
                self.console.set_preview(self.ops.screenshot())
                next_preview = time.monotonic() + self.preview_every_s
            self.ops.pump(250)  # keeps the browser responsive for the human
        return None

    def _note(self, event: str, **fields) -> None:
        if self.log is not None:
            self.log.event(event, **fields)
