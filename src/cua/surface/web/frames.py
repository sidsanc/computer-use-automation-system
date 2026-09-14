from importlib.resources import files

from playwright.sync_api import Frame, Page

from cua.surface.base import FrameNotFoundError

_HELPERS = files("cua.surface.web").joinpath("helpers.js").read_text(encoding="utf-8")
_INSTALL = f"() => {{ if (!window.__cuaH) window.__cuaH = {_HELPERS}; }}"


def install_helpers(frame: Frame) -> None:
    frame.evaluate(_INSTALL)


def frame_segment(frame: Frame) -> str:
    if frame.name:
        return frame.name
    siblings = frame.parent_frame.child_frames if frame.parent_frame else [frame]
    return f"#{siblings.index(frame)}"


def frame_path(frame: Frame) -> tuple[str, ...]:
    path: list[str] = []
    while frame.parent_frame is not None:
        path.insert(0, frame_segment(frame))
        frame = frame.parent_frame
    return tuple(path)


def frame_for(page: Page, path: tuple[str, ...]) -> Frame:
    frame = page.main_frame
    for segment in path:
        live = [f for f in frame.child_frames if not f.is_detached()]
        match = next((f for f in live if frame_segment(f) == segment), None)
        if match is None:
            raise FrameNotFoundError(f"frame {'/'.join(path)!r} not found (missing {segment!r})")
        frame = match
    return frame
