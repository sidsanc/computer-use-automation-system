"""Per-tenant specialisation of a shared capability.

Hundreds of institutions run the same vendor product with different branding, wording and
columns. Re-recording per tenant does not scale, so a capability is recorded once against
the product and a tenant may overlay *additions*: alternative locators and extra known
states. An overlay cannot change the contract (inputs, outputs, steps, order) or soften
risk, so a reviewer only has to read the diff, and drift shows up as a locator rank > 0.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from cua.schema.artifact import Capability, Handler
from cua.schema.base import IDENT, Strict
from cua.schema.targets import Css, LabelNeighbor, Locator, LocatorCandidate, RoleName, TableCell, Target, Text

ALIASABLE = ("name", "label", "column", "text")


class StepOverlay(Strict):
    prepend_locators: tuple[LocatorCandidate, ...] = ()


class Overlay(Strict):
    overlay_id: str = Field(pattern=IDENT)
    product: str = Field(pattern=IDENT)
    description: str = ""
    applies_to: tuple[str, ...] = Field(default=(), description="Capability ids; empty means every capability.")
    label_aliases: dict[str, str] = Field(
        default={}, description="Wording differences, e.g. 'Member Number' -> 'Account No.'")
    steps: dict[str, StepOverlay] = {}
    extra_handlers: tuple[Handler, ...] = ()

    @model_validator(mode="after")
    def _aliases_are_not_circular(self) -> "Overlay":
        clashes = sorted(set(self.label_aliases) & set(self.label_aliases.values()))
        if clashes:
            raise ValueError(f"label aliases must not map onto other keys: {clashes}")
        return self

    @classmethod
    def load(cls, path: Path) -> "Overlay":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class OverlayError(ValueError):
    pass


def _aliased(locator: Locator, aliases: dict[str, str]) -> Locator | None:
    """Return a copy of the locator using this tenant's wording, or None if nothing applies."""
    data: dict[str, Any] = locator.model_dump()
    changed = False
    for field in ALIASABLE:
        if isinstance(data.get(field), str) and data[field] in aliases:
            data[field] = aliases[data[field]]
            changed = True
    row = data.get("row")
    if isinstance(row, dict) and row.get("column") in aliases:
        row["column"] = aliases[row["column"]]
        changed = True
    if not changed:
        return None
    return type(locator).model_validate(data)


def _overlay_target(target: Target, overlay: Overlay, step_id: str) -> Target:
    prepend = list(overlay.steps.get(step_id, StepOverlay()).prepend_locators)
    for candidate in target.locators:
        if isinstance(candidate.locator, Css):
            continue
        alias = _aliased(candidate.locator, overlay.label_aliases)
        if alias is not None:
            prepend.append(LocatorCandidate(
                locator=alias, rationale=f"Tenant wording via overlay '{overlay.overlay_id}'; {candidate.rationale}"))
    if not prepend:
        return target
    return target.model_copy(update={"locators": (*prepend, *target.locators)})


def _overlay_conditions(conditions: tuple, overlay: Overlay, step_id: str) -> tuple:
    """Conditions that point at controls get the tenant's wording too, or a step would pass
    its action and then fail its own check on a reworded screen."""
    return tuple(
        c.model_copy(update={"target": _overlay_target(c.target, overlay, step_id)})
        if getattr(c, "kind", None) in ("element_present", "field_value") else c
        for c in conditions
    )


def apply(capability: Capability, overlay: Overlay) -> Capability:
    if overlay.product != capability.app.product:
        raise OverlayError(f"overlay '{overlay.overlay_id}' is for product '{overlay.product}', "
                           f"capability is '{capability.app.product}'")
    if overlay.applies_to and capability.id not in overlay.applies_to:
        raise OverlayError(f"overlay '{overlay.overlay_id}' does not apply to capability '{capability.id}'")
    unknown = sorted(set(overlay.steps) - {s.id for s in capability.steps})
    if unknown:
        raise OverlayError(f"overlay '{overlay.overlay_id}' references unknown steps: {unknown}")

    steps = tuple(
        step.model_copy(update={
            "target": step.target if step.target is None else _overlay_target(step.target, overlay, step.id),
            "pre": _overlay_conditions(step.pre, overlay, step.id),
            "post": _overlay_conditions(step.post, overlay, step.id),
        })
        for step in capability.steps
    )
    success = _overlay_conditions(capability.success, overlay, "success")
    handlers = (*capability.handlers, *overlay.extra_handlers)
    return capability.model_copy(update={"steps": steps, "success": success, "handlers": handlers})


def resolved_hash(capability: Capability) -> str:
    canonical = json.dumps(capability.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


__all__ = ["Overlay", "OverlayError", "StepOverlay", "apply", "resolved_hash",
           "RoleName", "LabelNeighbor", "TableCell", "Text"]
