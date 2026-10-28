"""Surface-agnostic observation model and targeting errors.

A web page, a legacy frameset or a desktop window (UIA/AX) all reduce to the same
shape: nodes with a role, a name and some structural context. Recorded flows only
ever talk to this shape, never to the DOM.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from cua.schema.conditions import Condition
from cua.schema.targets import Locator, Target

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
    unavailable_frames: tuple[str, ...] = ()  # could not be read this time (navigating); not "empty"

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
        if self.unavailable_frames:
            lines.append(f"[warning] frames still loading, observe again: {', '.join(self.unavailable_frames)}")
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


class ActionInfo(BaseModel):
    """Where an action would lead, as the policy gate needs to see it.

    On the web these are URLs and an HTTP method. A desktop surface maps the same idea onto
    its own vocabulary: the window or view being acted on, and the command a control invokes.
    """

    location_url: str
    control_name: str | None
    destination_url: str | None
    destination_method: Literal["GET", "POST"] | None


class Surface(Protocol):
    """Perceiving and acting on one live application, whatever it is made of.

    Eleven methods. A recorded flow never names a DOM, a window handle or a toolkit, so an
    implementation over Windows UIA or macOS AX satisfies this contract without the flow
    format or either runner changing.
    """

    lease: object | None  # ControlLease when a run can hand control to a human
    last_action: tuple[str, float] | None  # (kind, monotonic) — suppresses echoes of our own events

    def observe(self) -> Observation: ...

    def element(self, eid: str) -> object:
        """Resolve an ephemeral id from the latest observation to a live handle."""

    def resolve(self, target: Target) -> Resolution:
        """Walk the locator ladder. Exactly one match, or raise."""

    def target_for(self, eid: str) -> Target:
        """Derive ranked locators for an observed element, keeping only verified-unique ones."""

    def action_info(self, handle: object, kind: str, key: str | None = None) -> ActionInfo: ...

    def perform(self, kind: str, handle: object | None, value: str | None = None, key: str | None = None,
                timeout_s: float = 10, actor: str = "automation") -> str | None:
        """Carry out one action from the bounded vocabulary. Returns text for `extract`."""

    def check(self, condition: Condition, render: Callable[[str], str],
              value_of: Callable[[object], str]) -> bool: ...

    def dialogs(self) -> list[tuple[tuple[str, ...], str]]:
        """Visible modal surfaces, as (frame or window path, name)."""

    def frame_urls(self) -> dict[str, str]:
        """Each view's current location, keyed by path. Desktop: window or pane identities."""

    def settle(self, timeout_s: float = 10) -> bool:
        """Wait until the application stops changing, or the budget runs out."""

    def screenshot_masked(self, captions: list[str], values: list[str], columns: list[str] | None = None,
                          jpeg: bool = False) -> tuple[bytes, int]:
        """Capture with sensitive values hidden *before* the pixels exist. Returns (image, masked count)."""


class Capture(Protocol):
    """Records what a human did while they held control, with values masked at the source."""

    actions: list
    anomalies: list

    def announce(self) -> None:
        """Show the current control state to whoever is looking at the session."""

    def take_since(self, index: int) -> tuple: ...


class Session(Protocol):
    """One isolated run of one application: its surface, its lifecycle, its evidence."""

    surface: Surface
    blocked_requests: list[str]

    def open(self, location: str) -> None:
        """Go to an entry point — a URL, or a desktop screen reached by launching the app."""

    def idle(self, ms: int) -> None:
        """Let the application breathe without acting on it (polling, human-latency waits)."""

    def location(self) -> str: ...

    def title(self) -> str: ...

    def texts(self) -> list[str]:
        """Visible text per view, for things like reading a product version off the screen."""

    def install_capture(self, lease: object, on_event: Callable[[object, str], None]) -> Capture | None:
        """Start recording human actions, or return None if this surface cannot."""

    def announce(self) -> None:
        """Show who currently holds control, if the surface can display that."""

    def close(self, trace_path: Path | None = None) -> Path | None:
        """Tear down. Returns a recording path when one was produced."""


class SessionFactory(Protocol):
    """How a runner obtains a session without knowing what it is made of."""

    def __call__(self, allowed_origins: tuple[str, ...], *, video_dir: Path | None = None,
                 trace: bool = False) -> Session: ...


class SurfaceError(Exception):
    """Something went wrong driving the application. Implementations wrap their driver's
    exceptions in this, so the runners never import a driver to catch its errors."""

    category = "surface_error"


class TargetingError(SurfaceError):
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
