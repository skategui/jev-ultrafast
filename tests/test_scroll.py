"""SCROLL moves the container that holds the content, in a real headless Chrome. No model calls."""

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from jev_ultrafast import browser as harness
from jev_ultrafast.browser import browser_operation

# websockets is a locked dependency of browser-harness; the test talks CDP to its own Chrome, not the user's.
sync_client = pytest.importorskip("websockets.sync.client")
CHROME = next(
    (
        p
        for p in (
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        if p and Path(p).exists()
    ),
    None,
)

# The sheet sits away from the old fixed wheel point (550, 650), which lands on the backdrop.
MODAL = """<!doctype html><title>fixture</title><style>
body{margin:0} #page{height:3000px}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.5)}
#sheet{position:fixed;top:40px;left:40px;width:420px;height:400px;background:#fff;display:flex;flex-direction:column}
#body{flex:1;overflow:auto}
</style>
<div id="page"><p>Page behind</p></div>
<div id="overlay"></div>
<div id="sheet" role="dialog" aria-modal="true" aria-labelledby="title">
  <h2 id="title">Video</h2>
  <div id="body"><div style="height:360px">Intro</div>
    <button onclick="window.clicked=true">Validate</button><div style="height:1200px"></div></div>
</div>"""

# A locked page under a full-height sheet whose visible part is a textless video: nothing inside is in view yet.
SHEET = """<!doctype html><title>fixture</title><style>
body{margin:0;overflow:hidden} #page{height:3000px}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.5)}
#sheet{position:fixed;top:0;bottom:0;right:0;width:520px;background:#fff;overflow:auto}
</style>
<div id="page"><p>Page behind</p></div>
<div id="overlay"></div>
<div id="sheet" role="dialog" aria-label="Staff video">
  <div style="height:800px;background:#000"></div><button onclick="window.clicked=true">Approve</button>
  <div style="height:1200px"></div>
</div>"""

# A fixed-position menu escapes its overflow box: it is drawn, so it must stay clickable.
ESCAPE = """<!doctype html><title>fixture</title>
<div style="height:60px;overflow:auto"><div style="height:400px">Rows</div>
  <div role="menu" style="position:fixed;top:300px;left:40px">
    <button onclick="window.picked=true">Option A</button></div></div>"""

LONG = """<!doctype html><title>fixture</title><style>body{margin:0}</style>
<div style="height:3000px">Top</div>"""

WIDE = """<!doctype html><title>fixture</title><style>body{margin:0}td{padding:4px}</style>
<div id="wrap" style="width:600px;overflow-x:auto">
  <table style="width:2400px"><tr><td>Name</td><td>Action</td></tr>
    <tr><td>Ada</td><td style="text-align:right"><button onclick="window.edited=true">Edit</button></td></tr>
    <tr><td>Alan</td><td></td></tr><tr><td>Grace</td><td></td></tr></table>
</div>"""


@pytest.fixture
def cdp(tmp_path, monkeypatch):
    if CHROME is None:
        pytest.skip("Chrome is not installed")
    process = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={tmp_path}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = tmp_path / "DevToolsActivePort"
        until(lambda: port_file.exists() and len(port_file.read_text().split()) == 2, timeout=20)
        port = port_file.read_text().split()[0]
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
        ws = sync_client.connect(next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"], max_size=None)
        ids = iter(range(1, 1 << 30))

        def call(method, session_id=None, _response_timeout=None, **params):
            n = next(ids)
            ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while (message := json.loads(ws.recv(timeout=10))).get("id") != n:
                pass
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]

        monkeypatch.setattr(harness, "cdp", call)
        call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        yield call
        ws.close()
    finally:
        process.kill()
        process.wait()


def until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def evaluate(cdp, expression):
    return cdp("Runtime.evaluate", expression=expression, returnByValue=True)["result"].get("value")


def load(cdp, html):
    cdp("Page.navigate", url="data:text/html," + quote(html))
    until(lambda: evaluate(cdp, "document.title === 'fixture' && document.readyState === 'complete'"))


def observe():
    return browser_operation({"operation": "observe", "session": "test", "screenshot": False})


def act(action):
    return browser_operation({"operation": "act", "session": "test", "action": action, "text": None})


def control(page, action_id):
    return next(a for a in page["actions"] if a["id"] == action_id)


def labels(page):
    return [a["label"] for a in page["actions"]]


def test_scroll_moves_the_modal_body_and_not_the_page_behind(cdp):
    load(cdp, MODAL)
    page = observe()
    # Below the sheet's fold but inside the viewport: offered, it could only fail the click hit test.
    assert "Validate" not in labels(page)
    assert all(a.get("node") for a in page["actions"] if a["kind"] == "scroll"), "page behind is covered"
    down = control(page, "scroll_down")
    assert "Video" in down["label"]
    act(down)
    until(lambda: evaluate(cdp, "document.querySelector('#body').scrollTop") > 0)
    assert evaluate(cdp, "scrollY") == 0
    page = observe()
    act(next(a for a in page["actions"] if a["label"] == "Validate"))
    assert evaluate(cdp, "window.clicked") is True


def test_scroll_reaches_a_sheet_whose_visible_part_holds_no_text_or_control(cdp):
    load(cdp, SHEET)
    page = observe()
    down = control(page, "scroll_down")
    assert "Staff video" in down["label"]
    for _ in range(4):
        if "Approve" in labels(page):
            break
        act(control(page, "scroll_down"))
        page = observe()
    act(next(a for a in page["actions"] if a["label"] == "Approve"))
    assert evaluate(cdp, "window.clicked") is True
    assert evaluate(cdp, "scrollY") == 0


def test_a_fixed_menu_inside_a_scroller_is_still_offered(cdp):
    load(cdp, ESCAPE)
    act(next(a for a in observe()["actions"] if a["label"] == "Option A"))
    assert evaluate(cdp, "window.picked") is True


def test_scroll_still_moves_a_plain_long_document(cdp):
    load(cdp, LONG)
    down = control(observe(), "scroll_down")
    assert "node" not in down and down["delta"] == 560
    act(down)
    until(lambda: evaluate(cdp, "scrollY") > 0)


def test_a_scroll_locked_document_is_not_offered(cdp):
    # Modal libraries lock the page with overflow:hidden; a wheel there moves nothing.
    load(cdp, LONG.replace("body{margin:0}", "body{margin:0;overflow:hidden}"))
    assert not [a for a in observe()["actions"] if a["kind"] == "scroll"]


def test_scroll_right_brings_a_wide_table_column_into_view(cdp):
    load(cdp, WIDE)
    page = observe()
    for _ in range(6):
        if "Edit" in labels(page):
            break
        act(control(page, "scroll_right"))
        page = observe()
    assert evaluate(cdp, "document.querySelector('#wrap').scrollLeft") > 0
    act(next(a for a in page["actions"] if a["label"] == "Edit"))
    assert evaluate(cdp, "window.edited") is True
