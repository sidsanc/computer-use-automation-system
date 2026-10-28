from pathlib import Path

from playwright.sync_api import Browser, Page, Route
from playwright.sync_api import Error as PlaywrightError

from cua.paths import origin
from cua.surface.base import SurfaceError
from cua.surface.web.capture import HumanCapture
from cua.surface.web.surface import WebSurface


class WebSession:
    """One isolated browser context per run, fenced to the tenant's allowed origins at the network layer.

    The policy gate decides what the automation may *do*; this guard is defence in depth so
    that nothing on the page (a redirect, an embedded resource, a popup) reaches other hosts.
    """

    def __init__(
        self,
        browser: Browser,
        allowed_origins: tuple[str, ...],
        *,
        video_dir: Path | None = None,
        trace: bool = False,
    ) -> None:
        self.allowed_origins = allowed_origins
        self.blocked_requests: list[str] = []
        self.closed_popups: list[str] = []
        self._trace = trace
        self.context = browser.new_context(
            viewport={"width": 1280, "height": 860},
            accept_downloads=False,
            record_video_dir=str(video_dir) if video_dir else None,
        )
        self.context.route("**/*", self._guard)
        if trace:
            self.context.tracing.start(screenshots=True, snapshots=True)
        self.page: Page = self.context.new_page()
        self.context.on("page", self._close_popup)
        self.surface = WebSurface(self.page)
        self._capture: HumanCapture | None = None

    def _guard(self, route: Route) -> None:
        url = route.request.url
        if url.startswith(("data:", "about:", "blob:")) or origin(url) in self.allowed_origins:
            route.continue_()
        else:
            self.blocked_requests.append(origin(url))
            route.abort("blockedbyclient")

    def _close_popup(self, page: Page) -> None:
        if page is not self.page:
            self.closed_popups.append(page.url)
            page.close()

    # ---- the Session port ------------------------------------------------------------------

    def open(self, location: str) -> None:
        try:
            self.page.goto(location, wait_until="load")
        except PlaywrightError as exc:
            raise SurfaceError(str(exc).splitlines()[0]) from exc

    def idle(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def location(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def texts(self) -> list[str]:
        """Visible text of every frame, for things like reading a product version off the screen."""
        found = []
        for frame in self.page.frames:
            try:
                found.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except PlaywrightError:
                continue
        return found

    def install_capture(self, lease, on_event) -> HumanCapture:
        self._capture = HumanCapture(self.context, lease, on_event, self.surface)
        return self._capture

    def announce(self) -> None:
        if self._capture is not None:
            self._capture.announce(self.page)

    def close(self, trace_path: Path | None = None) -> Path | None:
        """Close the context; keep the trace only when a path is given. Returns the video path if recorded."""
        if self._trace:
            self.context.tracing.stop(path=str(trace_path) if trace_path else None)
        video = self.page.video
        self.context.close()
        return Path(video.path()) if video else None


class WebSessions:
    """Opens browser sessions. This is the only thing a runner needs to know about Playwright."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser

    def __call__(self, allowed_origins: tuple[str, ...], *, video_dir: Path | None = None,
                 trace: bool = False) -> WebSession:
        return WebSession(self.browser, allowed_origins, video_dir=video_dir, trace=trace)
