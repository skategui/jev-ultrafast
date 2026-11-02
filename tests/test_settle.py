"""Browser.settle() against a real headless Chrome and a local fixture server.

The real NET_PROBE, SETTLE_STATE, snapshot.js marker and settle() loop run in the page; only the
browser_harness daemon is replaced by a direct CDP websocket, so the user's Chrome is never touched.
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.client import connect

from jev_ultrafast.browser import NET_PROBE, Browser

PAGES = {
    # AC1: one request that never answers, static DOM.
    "/long-poll": "<p>Inbox</p><script>fetch('/hang')</script>",
    "/xhr-long-poll": "<p>Inbox</p><script>const x = new XMLHttpRequest(); x.open('GET', '/hang'); x.send()</script>",
    # AC2: a short request every second, forever, static DOM.
    "/ticker": "<p>Dashboard</p><script>setInterval(() => fetch('/tick'), 1000); fetch('/tick')</script>",
    # AC3: the page's own data arrives after ~1 s and changes the DOM 150 ms later, like a framework render.
    "/data": "<p id=out>Loading</p><script>fetch('/slow').then(r => r.text())"
    ".then(t => setTimeout(() => { document.getElementById('out').textContent = t }, 150))</script>",
}


def find_chrome():
    shell = "ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"
    found = [p for cache in ("~/Library/Caches", "~/.cache") for p in glob.glob(os.path.expanduser(f"{cache}/{shell}"))]
    found += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    found += [shutil.which(name) or "" for name in ("google-chrome", "chromium", "chromium-browser")]
    return os.environ.get("JEV_TEST_CHROME") or next((p for p in found if p and os.path.exists(p)), None)


@pytest.fixture(scope="module")
def server():
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/hang":
                release.wait()
            elif self.path == "/tick":
                time.sleep(0.1)
            elif self.path == "/slow":
                time.sleep(1.0)
            body = (PAGES.get(self.path) or "Loaded data").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path in PAGES else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    release.set()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def chrome():
    binary = find_chrome()
    if not binary:
        pytest.skip("no Chrome found: set JEV_TEST_CHROME to a Chrome or chrome-headless-shell binary")
    with tempfile.TemporaryDirectory() as profile:
        process = subprocess.Popen(
            [binary, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
             "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port_file, deadline = Path(profile) / "DevToolsActivePort", time.monotonic() + 15
            while not port_file.exists() or not port_file.read_text().strip():
                assert time.monotonic() < deadline, "Chrome did not open its debugging port"
                time.sleep(0.05)
            yield f"http://127.0.0.1:{port_file.read_text().split()[0]}"
        finally:
            process.terminate()
            process.wait(10)


class Cdp:
    def __init__(self, base):
        request = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
        self.target = json.load(urllib.request.urlopen(request))
        self.ws, self.next_id = connect(self.target["webSocketDebuggerUrl"], max_size=None), 0

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=10))
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})


@pytest.fixture
def opened(chrome, server):
    pages = []

    def open_page(path):
        cdp = Cdp(chrome)
        pages.append(cdp)
        page = Browser.__new__(Browser)
        page.call = cdp.call  # the real Browser.evaluate and settle() run over this connection
        page.call("Page.enable")
        page.call("Page.addScriptToEvaluateOnNewDocument", source=NET_PROBE)
        page.call("Page.navigate", url=server + path)
        deadline = time.monotonic() + 10
        while page.evaluate("document.readyState") != "complete":
            assert time.monotonic() < deadline, "fixture page did not load"
            time.sleep(0.02)
        return page

    yield open_page
    for cdp in pages:
        cdp.ws.close()
        urllib.request.urlopen(f"{chrome}/json/close/{cdp.target['id']}")


def timed_settle(page):
    started = time.monotonic()
    settled = page.settle()
    return settled, time.monotonic() - started


@pytest.mark.parametrize("path", ["/long-poll", "/xhr-long-poll"])
def test_a_request_that_never_answers_does_not_hold_settle(opened, path):
    settled, elapsed = timed_settle(opened(path))
    assert settled and elapsed < 1.5, (settled, elapsed)


def test_periodic_background_requests_do_not_hold_settle(opened):
    settled, elapsed = timed_settle(opened("/ticker"))
    assert settled and elapsed < 5, (settled, elapsed)


def test_settle_waits_for_the_data_the_page_is_loading(opened):
    page = opened("/data")
    settled, elapsed = timed_settle(page)
    assert settled, elapsed
    assert page.evaluate("document.getElementById('out').textContent") == "Loaded data"
    assert elapsed > 0.8, elapsed  # it really waited for the 1 s request, it did not return on a static DOM
