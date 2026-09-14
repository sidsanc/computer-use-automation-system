"""The seam between automation and people.

Discovery and replay never talk to an operator directly; they raise an InterventionRequest
through a HumanGate. Unattended runs use `UnattendedGate`, which records the request and
lets the run end as `needs_human` instead of blocking.
"""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field

from cua.schema.base import Strict

ReasonCode = Literal[
    "approval_required",
    "escalation_handler",
    "unknown_dialog",
    "recovery_exhausted",
    "unsafe_restart",
    "agent_requested",
    "no_progress",
    "action_errors",
]


class InterventionRequest(Strict):
    intervention_id: str
    run_id: str
    mode: Literal["discovery", "replay"]
    capability_id: str | None
    goal: str | None = None
    step_id: str | None
    step_intent: str | None = None
    reason_code: ReasonCode
    reason: str
    allowed: tuple[Literal["take_control", "approve", "deny", "abort"], ...]
    frame_urls: dict[str, str]
    screenshot: str | None = Field(default=None, description="Masked screenshot, relative to the run directory.")
    observation: str | None = Field(default=None, description="Redacted accessibility observation file.")
    created_at: datetime


class HumanAction(Strict):
    kind: str
    frame_path: tuple[str, ...]
    control: str
    value: str | None = None
    at: datetime


class HumanResolution(Strict):
    decision: Literal["approved", "denied", "resumed", "aborted", "unavailable"]
    operator: str | None = None
    note: str | None = None
    actions: tuple[HumanAction, ...] = ()


class HumanGate(Protocol):
    def request(self, request: InterventionRequest) -> HumanResolution: ...


class UnattendedGate:
    """No operator on the line: every request is recorded and answered 'unavailable'."""

    def __init__(self) -> None:
        self.requests: list[InterventionRequest] = []

    def request(self, request: InterventionRequest) -> HumanResolution:
        self.requests.append(request)
        return HumanResolution(decision="unavailable")
