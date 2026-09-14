"""How a recorded step identifies a control, independent of any live session.

Locators are listed most-robust first. Every candidate stored in an artifact
resolved to exactly one element when it was recorded; replay walks the list
and treats a match on more than one element as a hard failure, never picking the first.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
