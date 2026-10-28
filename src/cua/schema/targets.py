"""How a recorded step identifies a control, independent of any live session.

Locators are listed most-robust first. Every candidate stored in an artifact
resolved to exactly one element when it was recorded; replay walks the list
and treats a match on more than one element as a hard failure, never picking the first.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from cua.schema.base import Strict as _Strict


class RoleName(_Strict):
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str


class LabelNeighbor(_Strict):
    """Unlabelled control identified by the caption in the neighbouring table cell."""

    kind: Literal["label_neighbor"] = "label_neighbor"
    role: str
    label: str


class RowMatch(_Strict):
    column: str
    equals: str


class TableCell(_Strict):
    """Cell addressed by column header plus a key cell in the same row, never by its own value or index."""

    kind: Literal["table_cell"] = "table_cell"
    column: str
    row: RowMatch


class Text(_Strict):
    kind: Literal["text"] = "text"
    role: str | None = None
    text: str


class Css(_Strict):
    kind: Literal["css"] = "css"
    selector: str
    brittle: Literal[True] = True


Locator = Annotated[RoleName | LabelNeighbor | TableCell | Text | Css, Field(discriminator="kind")]


class LocatorCandidate(_Strict):
    locator: Locator
    rationale: str


class Target(_Strict):
    frame_path: tuple[str, ...] = ()
    locators: tuple[LocatorCandidate, ...] = Field(min_length=1)


def render_target(target: Target, render: Callable[[str], str]) -> Target:
    """Substitute `{{inputs.x}}` throughout a target's locators. No surface knowledge needed."""

    def one(locator: Locator) -> Locator:
        data = {k: render(v) if isinstance(v, str) else v for k, v in locator.model_dump().items()}
        if isinstance(data.get("row"), dict):
            data["row"] = {k: render(v) for k, v in data["row"].items()}
        return type(locator).model_validate(data)

    return target.model_copy(update={
        "locators": tuple(c.model_copy(update={"locator": one(c.locator)}) for c in target.locators)
    })
