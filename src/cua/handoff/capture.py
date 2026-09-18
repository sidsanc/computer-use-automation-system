from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files

from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError

from cua.handoff.control import ControlLease
from cua.handoff.gate import HumanAction

_SCRIPT = files("cua.handoff").joinpath("capture.js").read_text(encoding="utf-8")


class HumanCapture:
    """Records what a human does in the shared session, and shows who holds control.

    The banner is advisory: in a browser both parties can physically reach, the honest
    guarantee is that automation never acts without the lease, and that anything a human
    does while automation holds it is recorded as an anomaly rather than silently accepted.
    """

    def __init__(self, context: BrowserContext, lease: ControlLease, on_event: Callable[[HumanAction, str], None]):
        self.lease = lease
        self.on_event = on_event
        self.actions: list[HumanAction] = []
        self.anomalies: list[HumanAction] = []
        context.expose_binding("__cuaHumanEvent", lambda _source, payload: self._record(payload))
        context.add_init_script(_SCRIPT)

    def _record(self, payload: dict) -> None:
        frame = payload.get("frame") or "top"
        action = HumanAction(
            kind=str(payload.get("kind", "unknown")),
            frame_path=() if frame == "top" else (frame,),
            control=str(payload.get("control", "")),
            value=payload.get("value"),
            at=datetime.now(UTC),
        )
        holder = self.lease.holder
        (self.actions if holder == "human" else self.anomalies).append(action)
        self.on_event(action, holder)

    def announce(self, page) -> None:
        """Push the current control state into every frame (also call after navigation)."""
        for frame in page.frames:
            try:
                frame.evaluate("(state) => window.__cuaSetControl && window.__cuaSetControl(state)", self.lease.state)
            except PlaywrightError:
                continue

    def take_since(self, index: int) -> tuple[HumanAction, ...]:
        return tuple(self.actions[index:])
