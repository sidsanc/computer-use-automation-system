"""An in-memory application and surface, for testing the runners without a browser.

Two reasons this exists. It makes the core behaviours — handler precedence, business
outcomes, policy decisions, duplicate guards — testable in milliseconds instead of seconds,
because none of them are web concerns. And it is the executable definition of the Surface
and Session ports: anyone adding a desktop or terminal surface can read this to see exactly
what their implementation has to provide.
"""

import itertools
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cua.paths import path_matches
from cua.schema.conditions import (
    Condition,
    DialogPresent,
    ElementPresent,
    FieldValue,
    FrameUrl,
    TextVisible,
)
from cua.schema.targets import (
    Css,
    LabelNeighbor,
    LocatorCandidate,
    RoleName,
    TableCell,
    Target,
    Text,
    render_target,
)
from cua.surface.base import (
    ActionInfo,
    AmbiguousLocatorError,
    AXNode,
    FrameInfo,
    LocatorNotFoundError,
    Observation,
    Resolution,
    StaleElementError,
    SurfaceError,
)


@dataclass
class Control:
    """One thing on a screen. `label` stands in for a caption in the neighbouring cell."""

    role: str
    name: str
    value: str | None = None
    label: str | None = None
    column: str | None = None          # when the control is a table cell
    row_key: tuple[str, str] | None = None   # (column, value) identifying its row


@dataclass
class Screen:
    name: str
    path: str                     # what frame_urls() reports for this view
    controls: list[Control] = field(default_factory=list)
    text: str = ""                # free text for text_visible conditions
    dialog: str | None = None


@dataclass
class Move:
    """Acting on `control` while on `screen` lands on `to`."""

    screen: str
    control: str
    to: str
    kind: str = "click"
    writes: bool = False
    irreversible: bool = False


class FakeApp:
    """A handful of screens and the moves between them."""

    def __init__(self, screens: list[Screen], moves: list[Move], start: str, base_url: str = "https://fake.app",
                 title: str = "Fake teller") -> None:
        self.screens = {s.name: s for s in screens}
        self.moves = moves
        self.current = start
        self.base_url = base_url
        self.title = title
        self.history: list[str] = [start]
        self.writes: list[str] = []

    @property
    def screen(self) -> Screen:
        return self.screens[self.current]

    def move(self, control_name: str, kind: str) -> Move | None:
        for move in self.moves:
            if move.screen == self.current and move.control == control_name and move.kind == kind:
                return move
        return None

    def goto(self, path: str) -> None:
        """Navigate to the screen serving this path, as opening a URL would."""
        for screen in self.screens.values():
            if screen.path == path:
                self.current = screen.name
                self.history.append(screen.name)
                return
        raise SurfaceError(f"no screen serves {path!r}")

    def go(self, move: Move) -> None:
        self.current = move.to
        self.history.append(move.to)
        if move.writes:
            self.writes.append(f"{move.screen}:{move.control}")


class FakeSurface:
    """The Surface port over a FakeApp. No browser, no network, no timing."""

    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self.lease = None
        self.last_action: tuple[str, float] | None = None
        self._latest: Observation | None = None
        self._handles: dict[str, Control] = {}

    # ---- observation -------------------------------------------------------------------

    def observe(self) -> Observation:
        counter = itertools.count(1)
        self._handles = {}
        nodes = []
        screen = self.app.screen
        for control in screen.controls:
            eid = f"e{next(counter)}"
            self._handles[eid] = control
            nodes.append(AXNode(
                eid=eid, frame_path=("main",), role=control.role,
                name=control.name or (control.label or ""),
                name_source="neighbor_label" if control.label and not control.name else "native",
                value=control.value,
            ))
        if screen.dialog:
            nodes.append(AXNode(eid=None, frame_path=("main",), role="dialog", name=screen.dialog,
                                name_source="native", in_dialog=True))
        self._latest = Observation(
            observation_id=uuid.uuid4().hex[:8], title=self.app.title,
            frames=[FrameInfo(path=("main",), url=self.app.base_url + screen.path)], nodes=nodes,
        )
        return self._latest

    def element(self, eid: str) -> Control:
        if self._latest is None or eid not in self._handles:
            raise StaleElementError(eid)
        return self._handles[eid]

    def target_for(self, eid: str) -> Target:
        control = self.element(eid)
        locator = (LabelNeighbor(role=control.role, label=control.label) if control.label and not control.name
                   else RoleName(role=control.role, name=control.name))
        return Target(frame_path=("main",), locators=(LocatorCandidate(locator=locator, rationale="fake"),))

    # ---- targeting ---------------------------------------------------------------------

    def _matches(self, locator) -> list[Control]:
        controls = self.app.screen.controls
        match locator:
            case RoleName(role=role, name=name):
                return [c for c in controls if c.role == role and c.name == name]
            case LabelNeighbor(role=role, label=label):
                return [c for c in controls if c.role == role and c.label == label]
            case TableCell(column=column, row=row):
                return [c for c in controls if c.column == column and c.row_key == (row.column, row.equals)]
            case Text(text=text):
                return [c for c in controls if text in (c.name or "")]
            case Css(selector=selector):
                return [c for c in controls if c.name == selector]
        return []

    def resolve(self, target: Target) -> Resolution:
        for rank, candidate in enumerate(target.locators):
            found = self._matches(candidate.locator)
            if len(found) > 1:
                raise AmbiguousLocatorError(rank, candidate.locator, len(found))
            if found:
                return Resolution(rank=rank, locator=candidate.locator, handle=found[0])
        raise LocatorNotFoundError(f"nothing matched on screen {self.app.current!r}")

    # ---- acting ------------------------------------------------------------------------

    def action_info(self, handle: Control, kind: str, key: str | None = None) -> ActionInfo:
        move = self.app.move(handle.name, kind)
        here = self.app.base_url + self.app.screen.path
        if move is None:
            return ActionInfo(location_url=here, control_name=handle.name,
                              destination_url=None, destination_method=None)
        destination = self.app.base_url + self.app.screens[move.to].path
        return ActionInfo(location_url=here, control_name=handle.name, destination_url=destination,
                          destination_method="POST" if move.writes else "GET")

    def perform(self, kind: str, handle: Control | None, value: str | None = None, key: str | None = None,
                timeout_s: float = 10, actor: str = "automation") -> str | None:
        if self.lease is not None:
            self.lease.require(actor)
        self.last_action = (kind, time.monotonic())
        if kind == "extract":
            return handle.value if handle.value is not None else handle.name
        if kind in ("fill", "select") and handle is not None:
            handle.value = value
            return None
        move = self.app.move(handle.name, kind) if handle is not None else None
        if move is not None:
            self.app.go(move)
        return None

    # ---- conditions --------------------------------------------------------------------

    def check(self, condition: Condition, render: Callable[[str], str], value_of: Callable[[object], str]) -> bool:
        screen = self.app.screen
        match condition:
            case TextVisible(text=text):
                haystack = " ".join([screen.text, *(c.name or "" for c in screen.controls)])
                return render(text) in haystack
            case ElementPresent(target=target):
                try:
                    self.resolve(render_target(target, render))
                    return True
                except (LocatorNotFoundError, AmbiguousLocatorError):
                    return False
            case FieldValue(target=target, equals=expected):
                try:
                    return self.resolve(render_target(target, render)).handle.value == value_of(expected)
                except LocatorNotFoundError:
                    return False
            case FrameUrl(path=path):
                return path_matches(render(path), screen.path)
            case DialogPresent(name=name):
                return screen.dialog is not None and (name is None or screen.dialog == name)
        return False

    def dialogs(self) -> list[tuple[tuple[str, ...], str]]:
        return [(("main",), self.app.screen.dialog)] if self.app.screen.dialog else []

    def frame_urls(self) -> dict[str, str]:
        return {"main": self.app.base_url + self.app.screen.path}

    def settle(self, timeout_s: float = 10) -> bool:
        return True

    def screenshot_masked(self, captions: list[str], values: list[str], columns: list[str] | None = None,
                          jpeg: bool = False) -> tuple[bytes, int]:
        return f"fake-screenshot:{self.app.current}".encode(), 0


class FakeSession:
    """The Session port over a FakeApp."""

    def __init__(self, app: FakeApp, allowed_origins: tuple[str, ...] = ()) -> None:
        self.app = app
        self.surface = FakeSurface(app)
        self.allowed_origins = allowed_origins
        self.blocked_requests: list[str] = []
        self.opened: list[str] = []
        self.closed = False

    def open(self, location: str) -> None:
        self.opened.append(location)
        self.app.goto(location.removeprefix(self.app.base_url))

    def idle(self, ms: int) -> None:
        return None

    def location(self) -> str:
        return self.app.base_url + self.app.screen.path

    def title(self) -> str:
        return self.app.title

    def texts(self) -> list[str]:
        return [self.app.screen.text]

    def install_capture(self, lease, on_event) -> None:
        return None  # a fake app has no human to watch

    def announce(self) -> None:
        return None

    def close(self, trace_path: Path | None = None) -> Path | None:
        self.closed = True
        return None


class FakeSessions:
    """The SessionFactory port: hands out sessions over one in-memory app."""

    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self.sessions: list[FakeSession] = []

    def __call__(self, allowed_origins: tuple[str, ...], *, video_dir: Path | None = None,
                 trace: bool = False) -> FakeSession:
        session = FakeSession(self.app, allowed_origins)
        self.sessions.append(session)
        return session

