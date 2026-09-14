"""Result contract returned to the calling agent for every replay.

Exactly one terminal status. Recoveries are not a status: they are reported alongside
whichever status the run reached, so a caller never mistakes "we dismissed a notice" for
an answer, and "no such member" (business_outcome) is never confused with a crash (failure).
"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from cua.schema.artifact import Capability
from cua.schema.base import Strict

Status = Literal["success", "business_outcome", "needs_human", "policy_blocked", "failure"]
EXIT_CODES: dict[str, int] = {
    "success": 0,
    "business_outcome": 10,
    "needs_human": 20,
    "policy_blocked": 30,
    "failure": 40,
}
FailureCategory = Literal[
    "invalid_input",
    "precondition_failed",
    "locator_not_found",
    "ambiguous_locator",
    "postcondition_timeout",
    "app_error",
    "unexpected_state",
    "recovery_exhausted",
    "extraction_failed",
    "internal_error",
]


class CapabilityRef(Strict):
    id: str
    version: str
    resolved_hash: str
    overlay: str | None = None


class Outcome(Strict):
    code: str
    message: str
    step_id: str | None = None


class Failure(Strict):
    category: FailureCategory
    step_id: str | None
    expected: str
    observed: str
    evidence: dict[str, str] = {}


class PolicyDecision(Strict):
    rule: str
    step_id: str | None
    reason: str


class InterventionRef(Strict):
    intervention_id: str
    reason: str
    step_id: str | None


class RecoveryRecord(Strict):
    step_id: str | None
    handler: str
    recovery: str
    attempt: int


class CommittedEffect(Strict):
    step_id: str
    actor: Literal["automation", "human"]


class SideEffects(Strict):
    committed: tuple[CommittedEffect, ...] = ()
    possible: tuple[str, ...] = Field(default=(), description="Write steps dispatched without confirmation.")


class RunResult(Strict):
    run_id: str
    status: Status
    capability: CapabilityRef
    tenant: str
    inputs: dict[str, str]
    outputs: dict[str, Any] = {}
    outcome: Outcome | None = None
    failure: Failure | None = None
    policy: PolicyDecision | None = None
    intervention: InterventionRef | None = None
    recoveries_applied: tuple[RecoveryRecord, ...] = ()
    locator_ranks: dict[str, int] = {}
    side_effects: SideEffects = SideEffects()
    started_at: datetime
    duration_ms: int
    evidence_dir: str

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def drifted_steps(self) -> list[str]:
        return [step for step, rank in self.locator_ranks.items() if rank > 0]

    @model_validator(mode="after")
    def _status_payload(self) -> "RunResult":
        required = {
            "business_outcome": "outcome",
            "failure": "failure",
            "policy_blocked": "policy",
            "needs_human": "intervention",
        }
        for status, field in required.items():
            present = getattr(self, field) is not None
            if present != (self.status == status):
                raise ValueError(f"'{field}' must be set if and only if status is '{status}'")
        if self.status != "success" and self.outputs:
            raise ValueError("outputs are only returned on success")
        return self


class InvalidInputError(ValueError):
    pass


def validate_inputs(capability: Capability, raw: dict[str, str]) -> dict[str, str]:
    """Check caller inputs against the contract before touching the UI.

    A malformed input is a caller error (failure/invalid_input); the application's own
    validation messages, if it gets that far, are business outcomes.
    """
    problems = [f"missing input '{n}'" for n in capability.inputs if n not in raw]
    problems += [f"unexpected input '{n}'" for n in raw if n not in capability.inputs]
    for name, spec in capability.inputs.items():
        value = raw.get(name)
        if value is None:
            continue
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            problems.append(f"input '{name}' does not match {spec.pattern}")
        if spec.enum and value not in spec.enum:
            problems.append(f"input '{name}' must be one of {list(spec.enum)}")
        if spec.type == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(f"input '{name}' must be an integer")
        if spec.type == "money" and not re.fullmatch(r"\d+(\.\d{1,2})?", value):
            problems.append(f"input '{name}' must be a decimal amount like 25.00")
    if problems:
        raise InvalidInputError("; ".join(problems))
    return dict(raw)
