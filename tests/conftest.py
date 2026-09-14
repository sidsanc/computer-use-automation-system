import threading

import pytest
from werkzeug.serving import make_server

from mockbank.app import create_app

CREDS = ("teller01", "test-pass")


class LiveApp:
    def __init__(self, variant: str) -> None:
        self.app = create_app(variant, credentials=CREDS)
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def faults(self):
        return self.app.extensions["faults"]

    def stop(self) -> None:
        self._server.shutdown()


@pytest.fixture(scope="session")
def live_a():
    live = LiveApp("a")
    yield live
    live.stop()


@pytest.fixture(scope="session")
def live_b():
    live = LiveApp("b")
    yield live
    live.stop()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def signed_in_page(browser, live: LiveApp):
    page = browser.new_page()
    page.goto(f"{live.base_url}/login")
    page.locator("input[name=f1]").fill(CREDS[0])
    page.locator("input[name=f2]").fill(CREDS[1])
    with page.expect_navigation(url=f"{live.base_url}/teller"):
        page.locator("input[type=submit]").click()
    page.wait_for_load_state("load")
    return page
