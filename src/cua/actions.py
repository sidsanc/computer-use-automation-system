"""The single bounded action vocabulary.

The same types are the model's tools during discovery, the unit the policy gate
checks, the step kinds stored in artifacts, and what a Surface knows how to perform.
There is deliberately no free navigation, no coordinates and no raw script.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from cua.schema.base import IDENT, Strict
from cua.schema.conditions import Value

Effect = Literal["none", "navigation", "write"]


class Click(Strict):
    kind: Literal["click"] = "click"


class Fill(Strict):
    kind: Literal["fill"] = "fill"
    value: Value


class Select(Strict):
    """Choose an option by its visible label."""

    kind: Literal["select"] = "select"
    option: Value


class PressKey(Strict):
    kind: Literal["press_key"] = "press_key"
    key: Literal["Enter", "Tab", "Escape"]


class WaitFor(Strict):
    """Wait until the step's postconditions hold. There is no time-based wait."""

    kind: Literal["wait_for"] = "wait_for"


class Extract(Strict):
    kind: Literal["extract"] = "extract"
    output: str = Field(pattern=IDENT)


class RequestHuman(Strict):
    kind: Literal["request_human"] = "request_human"
    reason: str = Field(min_length=1)


class Finish(Strict):
    """Discovery only: the model claims the goal is met. The recorder verifies the claim."""

    kind: Literal["finish"] = "finish"
    summary: str


Action = Annotated[
    Click | Fill | Select | PressKey | WaitFor | Extract | RequestHuman | Finish, Field(discriminator="kind")
]
StepAction = Annotated[Click | Fill | Select | PressKey | WaitFor | Extract | RequestHuman, Field(discriminator="kind")]


@dataclass(frozen=True)
class ActionSpec:
    needs_target: bool
    default_effect: Effect
    description: str


ACTION_SPECS: dict[str, ActionSpec] = {
    "click": ActionSpec(True, "navigation", "Click a link, button or control."),
    "fill": ActionSpec(True, "none", "Type into a text field (replaces its content)."),
    "select": ActionSpec(True, "none", "Choose an option in a dropdown by its visible label."),
    "press_key": ActionSpec(True, "navigation", "Press Enter, Tab or Escape on a focused control."),
    "wait_for": ActionSpec(False, "none", "Wait for the expected state to appear."),
    "extract": ActionSpec(True, "none", "Read the text of an element into a declared output."),
    "request_human": ActionSpec(False, "none", "Stop and ask a human operator to take over."),
    "finish": ActionSpec(False, "none", "Declare the goal complete."),
}
