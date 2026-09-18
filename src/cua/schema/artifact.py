"""Capability artifact: a typed, versioned, reviewable contract for one replayable flow.

A capability is bound to a vendor *product* and version range, not to a tenant; tenants
reuse it through overlays. It carries no discovery-run values: anything run-specific is
an input reference, and `literal_leaks` lets the recorder prove it.
"""

import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import Field, model_validator

from cua.actions import ACTION_SPECS, Effect, Extract, Fill, RequestHuman, Select, StepAction
from cua.schema.base import IDENT, INPUT_TEMPLATE_RE, Strict
from cua.schema.conditions import Condition, InputRef, SecretRef
from cua.schema.targets import Target

SCHEMA_VERSION = "1.0"
SEMVER = r"^\d+\.\d+\.\d+$"
MONEY_RE = re.compile(r"^\(?-?\$?\s*([\d,]+(?:\.\d{1,2})?)\)?$")


class InputSpec(Strict):
    type: Literal["string", "integer", "money"]
    description: str
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    sensitivity: Literal["public", "pii"] = "public"


class OutputSpec(Strict):
    type: Literal["string", "integer", "money"]
    description: str
    currency: str | None = None
    sensitivity: Literal["public", "pii", "financial"] = "public"

    def parse(self, text: str) -> Any:
        text = " ".join(text.split())
        if self.type == "string":
            return text
        if self.type == "integer":
            return int(text.replace(",", ""))
        match = MONEY_RE.match(text)
        if not match:
            raise ValueError(f"not a money value: {text!r}")
        try:
            amount = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"not a money value: {text!r}") from exc
        if text.startswith("(") or "-" in text:
            amount = -amount
        return {"amount": str(amount), "currency": self.currency or "USD"}


class Recovery(Strict):
    kind: Literal["dismiss", "reauth_and_restart", "recheck"]
    target: Target | None = None

    @model_validator(mode="after")
    def _dismiss_needs_target(self) -> "Recovery":
        if (self.kind == "dismiss") != (self.target is not None):
            raise ValueError("dismiss recovery requires a target; other recoveries must not have one")
        return self


class Handler(Strict):
    """Detector for a known runtime state and what replay does about it.

    Precedence when several fire at once: escalate > hard_failure > business_outcome > recoverable.
    """

    code: str = Field(pattern=IDENT)
    kind: Literal["business_outcome", "recoverable", "escalate", "hard_failure"]
    when: tuple[Condition, ...] = Field(min_length=1)
    message: str
    recovery: Recovery | None = None
    max_attempts: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def _recovery_only_for_recoverable(self) -> "Handler":
        if (self.kind == "recoverable") != (self.recovery is not None):
            raise ValueError(f"handler {self.code}: recovery is required for, and only for, recoverable handlers")
        return self


class Step(Strict):
    id: str = Field(pattern=r"^s\d{2}_[a-z0-9_]+$")
    intent: str = Field(min_length=1)
    action: StepAction
    target: Target | None = None
    pre: tuple[Condition, ...] = ()
    post: tuple[Condition, ...] = ()
    effect: Effect
    irreversible: bool = False
    timeout_s: float = Field(default=10, gt=0, le=60)
    handlers: tuple[Handler, ...] = ()
    performed_by: Literal["automation", "human"] = "automation"

    @model_validator(mode="after")
    def _consistency(self) -> "Step":
        spec = ACTION_SPECS[self.action.kind]
        if spec.needs_target != (self.target is not None):
            verb = "requires" if spec.needs_target else "forbids"
            raise ValueError(f"step {self.id}: '{self.action.kind}' {verb} a target")
        if self.effect != "none" and not self.post:
            raise ValueError(f"step {self.id}: a step with effect '{self.effect}' must declare postconditions")
        if self.irreversible and self.effect != "write":
            raise ValueError(f"step {self.id}: only write steps can be irreversible")
        if self.action.kind == "wait_for" and not self.post:
            raise ValueError(f"step {self.id}: wait_for needs postconditions to wait for")
        return self


class AppBinding(Strict):
    product: str = Field(pattern=IDENT)
    versions: str = Field(description="Version range of the vendor product, e.g. '>=7.2,<8'.")


class Entry(Strict):
    path: str = Field(pattern=r"^/", description="Route relative to the tenant's base URL.")


class TokenUsage(Strict):
    input: int = 0
    output: int = 0


class Provenance(Strict):
    method: Literal["llm_discovery", "hand_authored"]
    recorded_at: datetime
    recorder_version: str
    run_id: str | None = None
    model_id: str | None = None
    prompt_template_hash: str | None = None
    step_count: int | None = None
    token_usage: TokenUsage | None = None
    transcript_ref: str | None = None


class Capability(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=SEMVER)
    status: Literal["draft", "approved"] = "draft"
    title: str
    description: str
    app: AppBinding
    entry: Entry
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}
    secrets: tuple[str, ...] = ()
    steps: tuple[Step, ...] = Field(min_length=1)
    success: tuple[Condition, ...] = Field(min_length=1)
    handlers: tuple[Handler, ...] = ()
    provenance: Provenance

    @model_validator(mode="after")
    def _references(self) -> "Capability":
        errors: list[str] = []
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            errors.append("step ids must be unique")
        for name in [*self.inputs, *self.outputs, *self.secrets]:
            if not re.match(IDENT, name):
                errors.append(f"invalid identifier: {name!r}")

        for path, value in _walk(self.model_dump(mode="python", exclude={"provenance"})):
            if isinstance(value, str):
                unknown = [m for m in INPUT_TEMPLATE_RE.findall(value) if m not in self.inputs]
                errors += [f"{path}: unknown input '{m}'" for m in unknown]
        for step in self.steps:
            value = step.action.value if isinstance(step.action, Fill) else getattr(step.action, "option", None)
            if isinstance(value, InputRef) and value.input not in self.inputs:
                errors.append(f"step {step.id}: unknown input '{value.input}'")
            if isinstance(value, SecretRef) and value.secret not in self.secrets:
                errors.append(f"step {step.id}: undeclared secret '{value.secret}'")
            if isinstance(step.action, Select) and isinstance(value, SecretRef):
                errors.append(f"step {step.id}: secrets can only be typed, not selected")

        extracted = [s.action.output for s in self.steps if isinstance(s.action, Extract)]
        for name in self.outputs:
            if extracted.count(name) != 1:
                errors.append(f"output '{name}' must be extracted by exactly one step (found {extracted.count(name)})")
        errors += [f"extract step writes undeclared output '{n}'" for n in extracted if n not in self.outputs]

        codes = [h.code for h in self.all_handlers()]
        if len(codes) != len(set(codes)):
            errors.append("handler codes must be unique across the capability")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def all_handlers(self) -> Iterator[Handler]:
        yield from self.handlers
        for step in self.steps:
            yield from step.handlers

    @property
    def risk(self) -> Literal["read", "write", "irreversible"]:
        if any(s.irreversible for s in self.steps):
            return "irreversible"
        return "write" if any(s.effect == "write" for s in self.steps) else "read"

    @property
    def requires_human(self) -> bool:
        return any(s.performed_by == "human" or isinstance(s.action, RequestHuman) for s in self.steps)

    def contract(self) -> dict[str, Any]:
        """What a calling agent needs: identity, typed inputs/outputs, risk and possible business outcomes."""

        def schema(specs: dict[str, InputSpec] | dict[str, OutputSpec]) -> dict[str, Any]:
            props = {}
            for name, spec in specs.items():
                prop: dict[str, Any] = {"description": spec.description}
                if spec.type == "money":
                    money = {"amount": {"type": "string"}, "currency": {"type": "string"}}
                    prop |= {"type": "object", "properties": money}
                else:
                    prop["type"] = spec.type
                for key in ("pattern", "enum"):
                    if getattr(spec, key, None):
                        prop[key] = getattr(spec, key)
                props[name] = prop
            return {"type": "object", "properties": props, "required": list(specs), "additionalProperties": False}

        return {
            "name": self.id,
            "version": self.version,
            "status": self.status,
            "title": self.title,
            "description": self.description,
            "risk": self.risk,
            "requires_human": self.requires_human,
            "inputs": schema(self.inputs),
            "outputs": schema(self.outputs),
            "business_outcomes": {h.code: h.message for h in self.all_handlers() if h.kind == "business_outcome"},
        }


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, value


def literal_leaks(capability: Capability, run_values: Iterable[str]) -> list[str]:
    """Paths of flow strings that contain a value observed or supplied during the recording run.

    The recorder calls this with discovery inputs and extracted outputs; any hit means a run-specific
    value was frozen into the flow, and the artifact must not be saved.

    The input/output declarations are the caller's contract, not recorded state, so a declared enum
    or a description may legitimately contain a value that this run happened to use. Personal data is
    the exception: a pii input must never carry its own value as an example.
    """
    needles = {v.strip() for v in run_values if v and len(v.strip()) >= 3}
    skip = {"provenance", "description", "title", "inputs", "outputs"}
    hits = [
        path for path, value in _walk(capability.model_dump(mode="python", exclude=skip))
        if isinstance(value, str) and any(n in value for n in needles)
    ]
    for name, spec in capability.inputs.items():
        if spec.sensitivity != "pii":
            continue
        declared = " ".join([spec.description, *(spec.enum or ())])
        hits += [f"inputs.{name}" for n in needles if n in declared]
    return hits
