"""Pre-action policy gate. Every action, whether chosen by the model, replayed from an
artifact or proposed by an assisted step, passes through `evaluate` before it is performed.

Risk is decided by policy from where the action leads, not by the artifact: an artifact
that labels a commit step as harmless cannot downgrade it.
"""

import re
from typing import Literal

from pydantic import Field, model_validator

from cua.paths import origin, path_matches
from cua.schema.base import Strict

ActionKind = Literal["click", "fill", "select", "press_key", "wait_for", "extract", "request_human", "finish"]
Risk = Literal["read", "navigation", "write", "irreversible"]
Verdict = Literal["allow", "block", "require_approval"]


class ProductPolicy(Strict):
    """Vendor-product knowledge, authored once and shared by every tenant running that product."""

    allowed_paths: tuple[str, ...] = Field(min_length=1)
    allowed_actions: tuple[ActionKind, ...] = Field(min_length=1)
    read_posts: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    irreversible_paths: tuple[str, ...] = ()
    irreversible_keywords: tuple[str, ...] = ()
    max_steps: int = Field(default=25, ge=1, le=200)

    def for_tenant(self, base_url: str, narrowing: "PolicyNarrowing") -> "Policy":
        return Policy(allowed_origins=(origin(base_url),), **self.model_dump()).narrowed(narrowing)


class Policy(ProductPolicy):
    allowed_origins: tuple[str, ...] = Field(min_length=1)

    def narrowed(self, narrowing: "PolicyNarrowing") -> "Policy":
        """Tenant policy may only remove permissions or add caution, never widen."""
        widened = [
            f"{name}: {sorted(set(extra) - set(getattr(self, name)))}"
            for name in ("allowed_paths", "allowed_actions", "read_posts")
            if (extra := getattr(narrowing, name)) is not None and not set(extra) <= set(getattr(self, name))
        ]
        if narrowing.max_steps is not None and narrowing.max_steps > self.max_steps:
            widened.append(f"max_steps: {narrowing.max_steps} > {self.max_steps}")
        if widened:
            raise PolicyWideningError("tenant policy tries to widen the base policy: " + "; ".join(widened))
        return self.model_copy(
            update={
                "allowed_paths": narrowing.allowed_paths or self.allowed_paths,
                "allowed_actions": narrowing.allowed_actions or self.allowed_actions,
                "read_posts": self.read_posts if narrowing.read_posts is None else narrowing.read_posts,
                "write_paths": self.write_paths + narrowing.extra_write_paths,
                "irreversible_paths": self.irreversible_paths + narrowing.extra_irreversible_paths,
                "irreversible_keywords": self.irreversible_keywords + narrowing.extra_irreversible_keywords,
                "max_steps": narrowing.max_steps or self.max_steps,
            }
        )


class PolicyNarrowing(Strict):
    allowed_paths: tuple[str, ...] | None = None
    allowed_actions: tuple[ActionKind, ...] | None = None
    read_posts: tuple[str, ...] | None = None
    extra_write_paths: tuple[str, ...] = ()
    extra_irreversible_paths: tuple[str, ...] = ()
    extra_irreversible_keywords: tuple[str, ...] = ()
    max_steps: int | None = None


class PolicyWideningError(ValueError):
    pass


class ActionContext(Strict):
    """What the gate needs to know about one concrete action, supplied by the surface."""

    action: ActionKind
    mode: Literal["discovery", "replay"]
    location_url: str = Field(description="URL of the frame the action happens in.")
    step_id: str | None = None
    control_name: str | None = None
    destination_url: str | None = Field(default=None, description="Link href or form action the action leads to.")
    destination_method: Literal["GET", "POST"] | None = None
    attended: bool = False
    capability_approved: bool = False
    allow_irreversible: bool = False

    @model_validator(mode="after")
    def _method_needs_destination(self) -> "ActionContext":
        if self.destination_method and not self.destination_url:
            raise ValueError("destination_method requires destination_url")
        return self


class Decision(Strict):
    verdict: Verdict
    risk: Risk
    rule: str
    reason: str


def _allowed_url(policy: Policy, url: str) -> bool:
    return origin(url) in policy.allowed_origins and any(path_matches(p, url) for p in policy.allowed_paths)


def classify(policy: Policy, ctx: ActionContext) -> tuple[Risk, str]:
    dest = ctx.destination_url
    if dest and any(path_matches(p, dest) for p in policy.irreversible_paths):
        return "irreversible", "destination matches an irreversible route"
    name = (ctx.control_name or "").lower()
    if dest and any(re.search(rf"\b{re.escape(k.lower())}\b", name) for k in policy.irreversible_keywords):
        return "irreversible", f"control name {ctx.control_name!r} looks like a commit action"
    if dest and any(path_matches(p, dest) for p in policy.write_paths):
        return "write", "destination matches a write route"
    if dest and ctx.destination_method == "POST":
        if any(path_matches(p, dest) for p in policy.read_posts):
            return "navigation", "POST to a route known to be read-only"
        return "write", "unclassified POST is treated as a write"
    if dest:
        return "navigation", "GET navigation"
    return "read", "no navigation or submission"


def evaluate(policy: Policy, ctx: ActionContext) -> Decision:
    if ctx.action not in policy.allowed_actions:
        return Decision(verdict="block", risk="read", rule="action_not_allowed",
                        reason=f"action '{ctx.action}' is not in the allowlist")
    if not _allowed_url(policy, ctx.location_url):
        return Decision(verdict="block", risk="read", rule="location_not_allowed",
                        reason="current page is outside the allowed origins/routes")
    if ctx.destination_url and not _allowed_url(policy, ctx.destination_url):
        return Decision(verdict="block", risk="navigation", rule="destination_not_allowed",
                        reason="action leads outside the allowed origins/routes")

    risk, why = classify(policy, ctx)
    if risk != "irreversible":
        return Decision(verdict="allow", risk=risk, rule="within_policy", reason=why)
    if ctx.mode == "replay" and ctx.capability_approved and ctx.allow_irreversible:
        return Decision(verdict="allow", risk=risk, rule="irreversible_preapproved",
                        reason=f"{why}; capability approved and caller allowed irreversible steps")
    if ctx.mode == "replay" and not ctx.attended:
        return Decision(verdict="block", risk=risk, rule="irreversible_unattended",
                        reason=f"{why}; unattended replay needs an approved capability and allow_irreversible")
    return Decision(verdict="require_approval", risk=risk, rule="irreversible_needs_human",
                    reason=f"{why}; a human must approve this step")
