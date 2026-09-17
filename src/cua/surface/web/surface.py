"""Web implementation of the surface: Chromium's accessibility tree, per frame.

Roles and names come from the browser's own accessibility tree (CDP), which is the
web analogue of UIA/AX on desktop. The DOM is only consulted for what legacy markup
fails to expose: captions in neighbouring cells and bold header rows.
"""

import itertools
import re
import time
import uuid
from collections.abc import Callable
from typing import Literal

from playwright.sync_api import ElementHandle, Frame, Page
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel

from cua.paths import path_matches
from cua.schema.conditions import Condition, DialogPresent, ElementPresent, FieldValue, FrameUrl, TextVisible
from cua.schema.targets import (
    Css,
    LabelNeighbor,
    Locator,
    LocatorCandidate,
    RoleName,
    RowMatch,
    TableCell,
    Target,
    Text,
)
from cua.surface.base import (
    CELL_ROLES,
    CONTAINER_ROLES,
    INTERACTIVE_ROLES,
    AmbiguousLocatorError,
    AXNode,
    FrameInfo,
    LocatorNotFoundError,
    Observation,
    Resolution,
    StaleElementError,
    TableContext,
    TargetingError,
)
from cua.surface.web.frames import frame_for, frame_path, install_helpers

KEEP_ROLES = INTERACTIVE_ROLES | CELL_ROLES | CONTAINER_ROLES | {"heading", "StaticText"}
TEXT_OWNER_ROLES = INTERACTIVE_ROLES | CELL_ROLES | {"heading", "option", "LabelText"}
# Chromium classifies table-for-layout markup (typical of legacy apps) with internal roles;
# locators and conditions use the standard ARIA role names.
ROLE_ALIASES = {"LayoutTable": "table", "LayoutTableRow": "row", "LayoutTableCell": "cell"}
NUMERIC_RE =re.compile(r"^[\s$€£%()+\-\d.,]+$")
MAX_TABLE_CELL_CANDIDATES = 2

_REGISTER = """function (key) {
  const w = this.ownerDocument.defaultView;
  if (!w.__cuaNodes || w.__cuaObs !== key.obs) { w.__cuaNodes = new Map(); w.__cuaObs = key.obs; }
  w.__cuaNodes.set(key.eid, this);
}"""
_ENRICH = """(obs) => {
  const out = {};
  if (window.__cuaObs !== obs || !window.__cuaNodes) return out;
  const order = window.__cuaH.documentOrder();
  for (const [key, node] of window.__cuaNodes) {
    out[key] = Object.assign(window.__cuaH.describe(node), { order: order.get(node) ?? 0 });
  }
  return out;
}"""
_LOOKUP = "([obs, eid]) => (window.__cuaObs === obs && window.__cuaNodes && window.__cuaNodes.get(eid)) || null"


def _role(ax_node: dict) -> str:
    role = ax_node.get("role", {}).get("value", "")
    return ROLE_ALIASES.get(role, role)


def _norm(text: str) -> str:
    return " ".join(text.split())


class ActionInfo(BaseModel):
    location_url: str
    control_name: str | None
    destination_url: str | None
    destination_method: Literal["GET", "POST"] | None


class WebSurface:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._cdp = page.context.new_cdp_session(page)
        self._latest: Observation | None = None

    # ---- observation -------------------------------------------------------------------------

    def observe(self) -> Observation:
        obs_id = uuid.uuid4().hex[:8]
        counter = itertools.count(1)
        tree = self._cdp.send("Page.getFrameTree")["frameTree"]
        frames: list[FrameInfo] = []
        nodes: list[AXNode] = []
        for cdp_frame_id, frame in self._paired_frames(tree, self.page.main_frame):
            path = frame_path(frame)
            frames.append(FrameInfo(path=path, url=frame.url))
            nodes += self._observe_frame(cdp_frame_id, frame, path, obs_id, counter)
        self._cdp.send("Runtime.releaseObjectGroup", {"objectGroup": "cua"})
        self._latest = Observation(
            observation_id=obs_id, title=self.page.title(), frames=frames, nodes=nodes
        )
        return self._latest

    def _paired_frames(self, cdp_node: dict, frame: Frame):
        yield cdp_node["frame"]["id"], frame
        unmatched = list(frame.child_frames)
        for child in cdp_node.get("childFrames", []):
            info = child["frame"]
            match = next(
                (f for f in unmatched if (info.get("name") and f.name == info["name"]) or f.url == info["url"]),
                None,
            )
            if match is not None:
                unmatched.remove(match)
                yield from self._paired_frames(child, match)

    def _observe_frame(self, cdp_frame_id, frame, path, obs_id, counter) -> list[AXNode]:
        ax_nodes = self._cdp.send("Accessibility.getFullAXTree", {"frameId": cdp_frame_id})["nodes"]
        by_id = {n["nodeId"]: n for n in ax_nodes}
        install_helpers(frame)
        raw: list[tuple[str, str, str, dict]] = []
        for n in ax_nodes:
            role = _role(n)
            backend_id = n.get("backendDOMNodeId")
            if n.get("ignored") or role not in KEEP_ROLES or backend_id is None:
                continue
            name = _norm(str(n.get("name", {}).get("value", "")))
            if role == "StaticText" and (not name or self._ancestor_roles(n, by_id) & TEXT_OWNER_ROLES):
                continue
            # Free text gets a private key (for ordering only); actionable nodes get an element id.
            key = f"t{next(counter)}" if role == "StaticText" else f"e{next(counter)}"
            obj = self._cdp.send("DOM.resolveNode", {"backendNodeId": backend_id, "objectGroup": "cua"})
            self._cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": obj["object"]["objectId"],
                    "functionDeclaration": _REGISTER,
                    "arguments": [{"value": {"obs": obs_id, "eid": key}}],
                },
            )
            raw.append((key, role, name, n))
        details = frame.evaluate(_ENRICH, obs_id)
        out = []
        for key, role, name, n in raw:
            d = details.get(key, {})
            name_source = "native" if name else "none"
            if not name and d.get("neighbor_label"):
                name, name_source = d["neighbor_label"], "neighbor_label"
            props = {p["name"]: p.get("value", {}).get("value") for p in n.get("properties", [])}
            out.append(
                AXNode(
                    eid=None if key.startswith("t") else key,
                    frame_path=path,
                    role=role,
                    name=name,
                    name_source=name_source,
                    value=d.get("value"),
                    disabled=bool(props.get("disabled")),
                    in_dialog=bool(d.get("in_dialog")),
                    table=TableContext(**d["table"]) if d.get("table") else None,
                    has_control=bool(d.get("has_control")),
                    order=d.get("order", 0),
                )
            )
        return sorted(out, key=lambda node: node.order)

    @staticmethod
    def _ancestor_roles(node: dict, by_id: dict) -> set[str]:
        roles = set()
        parent = by_id.get(node.get("parentId"))
        while parent is not None:
            roles.add(_role(parent))
            parent = by_id.get(parent.get("parentId"))
        return roles

    def element(self, eid: str) -> ElementHandle:
        if self._latest is None:
            raise StaleElementError(eid)
        node = self._latest.node(eid)
        frame = frame_for(self.page, node.frame_path)
        handle = frame.evaluate_handle(_LOOKUP, [self._latest.observation_id, eid]).as_element()
        if handle is None:
            raise StaleElementError(eid)
        return handle

    # ---- targeting ---------------------------------------------------------------------------

    def resolve(self, target: Target) -> Resolution:
        frame = frame_for(self.page, target.frame_path)
        for rank, candidate in enumerate(target.locators):
            handles = find(frame, candidate.locator)
            if len(handles) == 1:
                return Resolution(rank=rank, locator=candidate.locator, handle=handles[0])
            if len(handles) > 1:
                raise AmbiguousLocatorError(rank, candidate.locator, len(handles))
        tried = [c.locator.model_dump() for c in target.locators]
        raise LocatorNotFoundError(f"no locator matched in frame {target.frame_path}: {tried}")

    def target_for(self, eid: str) -> Target:
        """Derive ranked locators for an observed element, keeping only those that
        resolve uniquely to that same element right now."""
        assert self._latest is not None
        node = self._latest.node(eid)
        handle = self.element(eid)
        frame = frame_for(self.page, node.frame_path)
        kept: list[LocatorCandidate] = []
        for locator, rationale in _proposals(node, handle):
            try:
                handles = find(frame, locator)
            except PlaywrightError:
                continue
            if len(handles) == 1 and handles[0].evaluate("(a, b) => a === b", handle):
                kept.append(LocatorCandidate(locator=locator, rationale=rationale))
        if not kept:
            raise TargetingError(f"could not derive a unique locator for {eid} ({node.role} {node.name!r})")
        return Target(frame_path=node.frame_path, locators=tuple(kept))


    # ---- acting ------------------------------------------------------------------------------

    def action_info(self, handle: ElementHandle, kind: str, key: str | None = None) -> ActionInfo:
        frame = handle.owner_frame()
        install_helpers(frame)
        info = handle.evaluate("(el, [kind, key]) => window.__cuaH.actionInfo(el, kind, key)", [kind, key])
        return ActionInfo(location_url=frame.url, control_name=info["name"] or None,
                          destination_url=info["url"], destination_method=info["method"])

    def perform(self, kind: str, handle: ElementHandle | None, value: str | None = None, key: str | None = None,
                timeout_s: float = 10) -> str | None:
        timeout = timeout_s * 1000
        match kind:
            case "click":
                handle.click(timeout=timeout)
            case "fill":
                handle.fill(value or "", timeout=timeout)
            case "select":
                handle.select_option(label=value, timeout=timeout)
            case "press_key":
                handle.press(key, timeout=timeout)
            case "extract":
                return handle.inner_text()
            case _:
                raise ValueError(f"surface cannot perform '{kind}'")
        return None

    # ---- conditions ----------------------------------------------------------------------------

    def check(self, condition: Condition, render: Callable[[str], str], value_of: Callable[[object], str]) -> bool:
        """Evaluate against the live, untrimmed page. Transient errors (a frame mid-navigation) read as 'not yet'."""
        try:
            return self._check(condition, render, value_of)
        except (PlaywrightError, TargetingError):
            return False

    def _check(self, condition: Condition, render, value_of) -> bool:
        match condition:
            case TextVisible(text=text, frame_path=path, match=mode):
                frames = [frame_for(self.page, path)] if path is not None else self.page.frames
                wanted = re.compile(render(text)) if mode == "regex" else render(text)
                for frame in frames:
                    loc = frame.get_by_text(wanted, exact=mode == "exact")
                    if any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5))):
                        return True
                return False
            case ElementPresent(target=target):
                frame = frame_for(self.page, target.frame_path)
                return any(find(frame, _rendered(c.locator, render)) for c in target.locators)
            case FieldValue(target=target, equals=expected):
                handle = self.resolve(render_target(target, render)).handle
                return handle.input_value() == value_of(expected)
            case FrameUrl(frame_path=path, path=pattern):
                return path_matches(render(pattern), frame_for(self.page, path).url)
            case DialogPresent(name=name):
                return any(name is None or d == name for _, d in self.dialogs())
        raise TypeError(f"unsupported condition: {condition!r}")

    def dialogs(self) -> list[tuple[tuple[str, ...], str]]:
        found = []
        for frame in self.page.frames:
            for role in ("dialog", "alertdialog"):
                loc = frame.get_by_role(role)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible():
                        found.append((frame_path(frame), item.get_attribute("aria-label") or ""))
        return found

    def frame_urls(self) -> dict[str, str]:
        return {"/".join(frame_path(f)) or "top": f.url for f in self.page.frames}

    def settle(self, timeout_s: float = 10) -> bool:
        """Wait until every frame has finished loading and its text has stopped changing."""
        deadline = time.monotonic() + timeout_s
        previous = None
        while time.monotonic() < deadline:
            try:
                probe = "() => [document.readyState, document.body ? document.body.innerText.length : 0]"
                snapshot = tuple((f.url, f.evaluate(probe)) for f in self.page.frames)
            except PlaywrightError:
                snapshot = None
            ready = snapshot is not None and all(state[0] == "complete" for _, state in snapshot)
            if ready and snapshot == previous:
                return True
            previous = snapshot
            self.page.wait_for_timeout(250)
        return False

    def screenshot_masked(self, captions: list[str], values: list[str], jpeg: bool = False) -> tuple[bytes, int]:
        masked = 0
        for frame in self.page.frames:
            try:
                install_helpers(frame)
                masked += frame.evaluate("([c, v]) => window.__cuaH.mask(c, v)", [captions, values])
            except PlaywrightError:
                continue
        try:
            if jpeg:
                return self.page.screenshot(type="jpeg", quality=60), masked
            return self.page.screenshot(full_page=True), masked
        finally:
            for frame in self.page.frames:
                try:
                    frame.evaluate("() => window.__cuaH && window.__cuaH.unmask()")
                except PlaywrightError:
                    continue


def _rendered(locator: Locator, render: Callable[[str], str]) -> Locator:
    data = locator.model_dump()
    rendered = {k: render(v) if isinstance(v, str) else v for k, v in data.items()}
    if isinstance(data.get("row"), dict):
        rendered["row"] = {k: render(v) for k, v in data["row"].items()}
    return type(locator).model_validate(rendered)


def render_target(target: Target, render: Callable[[str], str]) -> Target:
    """Substitute ``{{inputs.x}}`` in every locator of a target."""
    return target.model_copy(update={
        "locators": tuple(c.model_copy(update={"locator": _rendered(c.locator, render)}) for c in target.locators)
    })


def _proposals(node: AXNode, handle: ElementHandle) -> list[tuple[Locator, str]]:
    proposals: list[tuple[Locator, str]] = []
    if node.name_source == "neighbor_label":
        proposals.append((
            LabelNeighbor(role=node.role, label=node.name),
            "Control has no accessible name; the caption in the neighbouring cell is what an "
            "operator reads and survives styling and layout changes.",
        ))
    elif node.name and node.role not in CELL_ROLES:
        proposals.append((
            RoleName(role=node.role, name=node.name),
            "Accessible role and name; independent of markup structure and styling.",
        ))
    if node.role in CELL_ROLES and node.table and node.table.column:
        keys = [
            (col, val) for col, val in node.table.row_values.items()
            if col != node.table.column and val and not NUMERIC_RE.match(val)
        ]
        for col, val in keys[:MAX_TABLE_CELL_CANDIDATES]:
            proposals.append((
                TableCell(column=node.table.column, row=RowMatch(column=col, equals=val)),
                "Addressed by column header plus a key cell in the same row; independent of row "
                "order and of the cell's own (changing) value.",
            ))
    if node.role in CELL_ROLES and node.name:
        proposals.append((
            Text(role="cell", text=node.name),
            "Matches the cell's current text; only valid for static captions, never for values.",
        ))
    install_helpers(handle.owner_frame())
    proposals.append((
        Css(selector=handle.evaluate("el => window.__cuaH.cssPath(el)")),
        "Structural path; brittle under layout changes, last resort.",
    ))
    return proposals


def find(frame: Frame, locator: Locator) -> list[ElementHandle]:
    match locator:
        case RoleName(role=role, name=name):
            return frame.get_by_role(role, name=name, exact=True).element_handles()
        case LabelNeighbor(role=role, label=label):
            install_helpers(frame)
            return [
                h for h in frame.get_by_role(role).element_handles()
                if h.evaluate("el => window.__cuaH.neighborLabel(el)") == label
            ]
        case TableCell(column=column, row=row):
            install_helpers(frame)
            array = frame.evaluate_handle(
                "([c, k, v]) => window.__cuaH.findTableCells(c, k, v)", [column, row.column, row.equals]
            )
            return [el for p in array.get_properties().values() if (el := p.as_element())]
        case Text(role=role, text=text):
            if role:
                exact = re.compile(rf"^\s*{re.escape(text)}\s*$")
                return frame.get_by_role(role).filter(has_text=exact).element_handles()
            return frame.get_by_text(text, exact=True).element_handles()
        case Css(selector=selector):
            return frame.locator(selector).element_handles()
    raise TypeError(f"unsupported locator: {locator!r}")
