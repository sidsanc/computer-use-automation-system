"""Surface-agnostic observation model and targeting errors.

A web page, a legacy frameset or a desktop window (UIA/AX) all reduce to the same
shape: nodes with a role, a name and some structural context. Recorded flows only
ever talk to this shape, never to the DOM.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel

from cua.schema.targets import Locator

NameSource = Literal["native", "neighbor_label", "none"]
INTERACTIVE_ROLES = {"link", "button", "textbox", "searchbox", "combobox", "listbox", "checkbox", "radio"}
VALUE_ROLES = {"textbox", "searchbox", "combobox", "listbox"}
CELL_ROLES ={"cell", "gridcell", "columnheader", "rowheader"}
CONTAINER_ROLES = {"dialog", "alertdialog", "alert"}


class TableContext(BaseModel):
    table: int
    row: int
    col: int
    column: str | None = None
    row_values: dict[str, str] = {}


class AXNode(BaseModel):
    eid: str | None  # ephemeral, valid only for the observation that produced it
    frame_path: tuple[str, ...]
    role: str
    name: str
    name_source: NameSource
    value: str | None = None
    disabled: bool = False
    in_dialog: bool = False
    table: TableContext | None = None  # for controls: the table cell that contains them
    has_control: bool = False
    order: int = 0


class FrameInfo(BaseModel):
    path: tuple[str, ...]
    url: str


class Observation(BaseModel):
    observation_id: str
    title: str
    frames: list[FrameInfo]
    nodes: list[AXNode]

    def node(self, eid: str) -> AXNode:
        for n in self.nodes:
            if n.eid == eid:
                return n
        raise StaleElementError(eid)

    def digest(self) -> str:
        """Stable fingerprint of what is on screen, used for no-progress detection."""
        parts = [f"{f.path}|{f.url}" for f in self.frames]
        parts += [f"{n.frame_path}|{n.role}|{n.name}|{n.value}" for n in self.nodes]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def render(self, max_rows_per_table: int = 25) -> str:
        """Trimmed text view for the model. Condition checks never use this."""
        lines = [f"Page title: {self.title}"]
        for frame in self.frames:
            nodes = [n for n in self.nodes if n.frame_path == frame.path]
            if not nodes:
                continue
            lines.append(f"[frame {'/'.join(frame.path) or 'top'}] {frame.url}")
            # Entries keep document order: a plain node line, or a table row collecting its cells.
            entries: list[str | list[AXNode]] = []
            rows: dict[tuple[int, int], list[AXNode]] = {}
            hidden_rows: dict[int, int] = {}
            for n in nodes:
                if n.has_control:
                    continue  # the controls inside the cell are rendered in its place
                if n.table is None:
                    entries.append(_render_node(n))
                    continue
                key = (n.table.table, n.table.row)
                if key not in rows:
                    shown = sum(1 for t, _ in rows if t == n.table.table)
                    if shown >= max_rows_per_table:
                        hidden_rows[n.table.table] = hidden_rows.get(n.table.table, 0) + 1
                        rows[key] = []
                        continue
                    rows[key] = []
                    entries.append(rows[key])
                rows[key].append(n)
            for entry in entries:
                lines.append(entry if isinstance(entry, str) else "  row: " + " | ".join(map(_render_cell, entry)))
            lines += [f"  ... {count} more rows in table {t}" for t, count in hidden_rows.items()]
        return "\n".join(lines)


def _render_node(n: AXNode) -> str:
    text = f"  {n.eid + ' ' if n.eid else ''}{n.role} \"{n.name}\""
    if n.name_source == "neighbor_label":
        text += " (named from neighbouring label)"
    if n.value is not None and n.role in VALUE_ROLES:
        text += f' value="{n.value}"'
    if n.disabled:
        text += " [disabled]"
    if n.in_dialog and n.role not in CONTAINER_ROLES:
        text += " [in dialog]"
    return text


def _render_cell(n: AXNode) -> str:
    if n.role in INTERACTIVE_ROLES:
        return _render_node(n).strip()
    return f'{n.eid} "{n.name}"' if n.eid else f'"{n.name}"'


class Resolution(BaseModel, arbitrary_types_allowed=True):
    rank: int
    locator: Locator
    handle: object


class TargetingError(Exception):
    category = "targeting_error"


class StaleElementError(TargetingError):
    category = "stale_element"

    def __init__(self, eid: str) -> None:
        super().__init__(f"element id {eid!r} is not in the current observation; observe again")


class FrameNotFoundError(TargetingError):
    category = "frame_not_found"


class LocatorNotFoundError(TargetingError):
    category = "locator_not_found"


class AmbiguousLocatorError(TargetingError):
    category = "ambiguous_locator"

    def __init__(self, rank: int, locator: Locator, count: int) -> None:
        super().__init__(f"locator #{rank} {locator.model_dump()} matched {count} elements")
        self.rank, self.locator, self.count = rank, locator, count
