"""Turn a discovery run into a capability artifact, decoupled from the model transcript.

The recorder never trusts the transcript for *how* to find things: locators and
postconditions were derived from the live page at the moment of each action. Its job is to
assemble them, strip anything that depends on this run's values, and refuse to save an
artifact that would replay only for the member it was recorded with.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cua.discovery.goal import GoalSpec
from cua.schema.artifact import AppBinding, Capability, Entry, Provenance, Step, TokenUsage, literal_leaks
from cua.schema.conditions import Condition, ElementPresent, Value
from cua.schema.targets import Target

RECORDER_VERSION = "1.0"


class RecordingError(ValueError):
    pass


@dataclass
class RecordedAction:
    kind: str
    intent: str
    target: Target | None
    risk: str
    post: tuple[Condition, ...] = ()
    value: Value | None = None
    key: str | None = None
    output: str | None = None
    performed_by: str = "automation"


@dataclass
class RunFacts:
    run_id: str
    model_id: str
    prompt_template_hash: str
    input_tokens: int
    output_tokens: int
    run_values: list[str] = field(default_factory=list)


def slug(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return words[:limit].rstrip("_") or "step"


def strip_run_values(target: Target, run_values: list[str]) -> Target | None:
    needles = [v for v in run_values if v and len(v) >= 3]
    kept = tuple(c for c in target.locators if not any(n in json.dumps(c.locator.model_dump()) for n in needles))
    return target.model_copy(update={"locators": kept}) if kept else None


def build_capability(
    goal: GoalSpec,
    app_product: str,
    app_version: str | None,
    actions: list[RecordedAction],
    final_frames: tuple[Condition, ...],
    facts: RunFacts,
    version: str,
) -> Capability:
    steps = []
    for index, action in enumerate(actions, start=1):
        target = None
        if action.target is not None:
            target = strip_run_values(action.target, facts.run_values)
            if target is None:
                raise RecordingError(f"step {index} ({action.intent!r}): every locator depended on run values")
        effect = {"read": "none", "navigation": "navigation"}.get(action.risk, "write")
        if effect != "none" and not action.post:
            if effect == "write":
                raise RecordingError(f"step {index} ({action.intent!r}): no observable postcondition for a write")
            effect = "none"
        payload: dict = {"kind": action.kind}
        if action.kind == "fill":
            payload["value"] = action.value
        elif action.kind == "select":
            payload["option"] = action.value
        elif action.kind == "press_key":
            payload["key"] = action.key
        elif action.kind == "extract":
            payload["output"] = action.output
        elif action.kind == "request_human":
            payload["reason"] = action.intent
        steps.append(Step(
            id=f"s{index:02d}_{slug(action.intent)}", intent=action.intent, action=payload, target=target,
            post=action.post if effect != "none" else (), effect=effect,
            irreversible=action.risk == "irreversible", performed_by=action.performed_by,
        ))

    success = list(final_frames)
    for step in steps:
        if step.action.kind == "extract" and step.target is not None:
            success.append(ElementPresent(target=step.target))
    if not success:
        raise RecordingError("no success condition could be derived from the final state")

    capability = Capability(
        id=goal.capability_id,
        version=version,
        status="draft",
        title=goal.title,
        description=goal.goal,
        app=AppBinding(product=app_product, versions=_version_range(app_version)),
        entry=Entry(path=goal.entry),
        inputs=goal.inputs,
        outputs=goal.outputs,
        secrets=(),
        steps=tuple(steps),
        success=tuple(success),
        provenance=Provenance(
            method="llm_discovery", recorded_at=datetime.now(UTC), recorder_version=RECORDER_VERSION,
            run_id=facts.run_id, model_id=facts.model_id, prompt_template_hash=facts.prompt_template_hash,
            step_count=len(steps), token_usage=TokenUsage(input=facts.input_tokens, output=facts.output_tokens),
            transcript_ref="events.jsonl",
        ),
    )
    leaks = literal_leaks(capability, facts.run_values)
    if leaks:
        raise RecordingError(f"run-specific values were frozen into the artifact at: {leaks}")
    return capability


def _version_range(app_version: str | None) -> str:
    if not app_version:
        return ">=0"
    major, minor, *_ = app_version.split(".")
    return f">={major}.{minor},<{int(major) + 1}"


def next_version(directory: Path) -> str:
    stems = [f.stem for f in directory.glob("*.json") if re.fullmatch(r"\d+\.\d+\.\d+", f.stem)]
    existing = sorted(tuple(int(p) for p in stem.split(".")) for stem in stems)
    if not existing:
        return "1.0.0"
    major, minor, _ = existing[-1]
    return f"{major}.{minor + 1}.0"


def save(capability: Capability, root: Path) -> Path:
    directory = root / capability.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{capability.version}.json"
    if path.exists():
        raise RecordingError(f"{path} already exists; versions are immutable")
    path.write_text(capability.model_dump_json(indent=2, exclude_defaults=False) + "\n", encoding="utf-8")
    return path
