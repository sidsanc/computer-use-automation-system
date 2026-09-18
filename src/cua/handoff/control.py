"""Who is allowed to act on the live session, as an explicit state machine.

Exactly one holder at a time. Automation cannot act while a human holds control, and the
human's input is blocked while automation holds it, so "who is in control" is enforced
rather than merely recorded. Every transition is logged with the actor.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

State = Literal["automation", "paused", "human", "resuming", "closed"]
Holder = Literal["automation", "human", "nobody"]

LEGAL: dict[State, set[State]] = {
    "automation": {"paused", "closed"},
    "paused": {"human", "automation", "closed"},  # back to automation = the human declined to take over
    "human": {"resuming", "closed"},
    "resuming": {"automation", "paused", "closed"},  # back to paused = resume verification failed
    "closed": set(),
}
HOLDER: dict[State, Holder] = {
    "automation": "automation", "paused": "nobody", "human": "human", "resuming": "nobody", "closed": "nobody",
}


class ControlError(RuntimeError):
    pass


@dataclass
class Transition:
    at: datetime
    frm: State
    to: State
    actor: str
    note: str | None = None


@dataclass
class ControlLease:
    state: State = "automation"
    intervention_id: str | None = None
    operator: str | None = None
    history: list[Transition] = field(default_factory=list)
    on_change: Callable[[Transition], None] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def holder(self) -> Holder:
        return HOLDER[self.state]

    def to(self, state: State, actor: str, note: str | None = None) -> Transition:
        with self._lock:
            if state not in LEGAL[self.state]:
                raise ControlError(f"illegal control transition {self.state} -> {state}")
            transition = Transition(at=datetime.now(UTC), frm=self.state, to=state, actor=actor, note=note)
            self.state = state
            if state in ("automation", "closed"):
                self.intervention_id = None
            self.history.append(transition)
            if self.on_change:
                self.on_change(transition)
            return transition

    def require(self, who: Holder) -> None:
        with self._lock:
            if self.holder != who:
                raise ControlError(f"{who} tried to act while control is held by {self.holder} (state {self.state})")

    def pause(self, intervention_id: str, actor: str = "automation") -> Transition:
        with self._lock:
            transition = self.to("paused", actor, note=f"intervention {intervention_id}")
            self.intervention_id = intervention_id
            return transition

    def take(self, operator: str) -> Transition:
        with self._lock:
            transition = self.to("human", operator, note="operator took control")
            self.operator = operator
            return transition

    def hand_back(self, actor: str) -> Transition:
        return self.to("resuming", actor, note="operator handed control back")
