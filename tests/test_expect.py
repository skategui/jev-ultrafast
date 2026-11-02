"""--expect / --expect-in against snapshot.js run in a real headless Chrome. Local file, no model calls."""

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint

SNAPSHOT_JS = (Path(loop.__file__).with_name("snapshot.js")).read_text()
CHROME_PATHS = (
    os.environ.get("BH_CHROME_PATH") or "",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)
CHROME = next((p for p in CHROME_PATHS if p and os.path.exists(p)), None)
# The observed page must be big enough that every fixture element sits inside the viewport.
WINDOW = "--window-size=1280,900"

# Only the filter dropdown says "Catégories"; the client panel is closed (display:none) or lacks the text.
DROPDOWN = """<header><button aria-expanded="true">Filtres</button>
<ul role="listbox" aria-label="Filtres"><li role="option">Catégories</li><li role="option">Tags</li></ul></header>"""
HIDDEN = """<p style="display:none">Masqué CSS</p><p hidden>Masqué attribut</p>
<p aria-hidden="true">Masqué ARIA</p><p style="visibility:hidden">Masqué visibilité</p>
<p style="opacity:0">Masqué opacité</p><p>Visible ici</p>"""


def observe(tmp_path, body):
    """The page dict snapshot.js returns for `body`, read by a real headless Chrome."""
    if CHROME is None:
        pytest.skip("no Chrome/Chromium found (set BH_CHROME_PATH)")
    html = tmp_path / "page.html"
    html.write_text(
        "<!doctype html><meta charset=utf-8><title>Fixture</title><body>" + body
        + "<script>{const s=" + SNAPSHOT_JS + ";const o=document.createElement('pre');o.id='jev-out';"
        "o.textContent=JSON.stringify(s);document.body.append(o);}</script></body>",
        encoding="utf-8",
    )
    dom = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", WINDOW, "--dump-dom", html.as_uri()],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    match = re.search(r'<pre id="jev-out">(.*?)</pre>', dom, re.S)
    assert match, dom[-2000:]
    return json.loads(match.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def missing(page, expect=(), expect_in=()):
    a = loop.Agent.__new__(loop.Agent)
    a.expect, a.expect_in = tuple(expect), tuple(expect_in)
    return a.missing(page)


def test_unscoped_expect_counts_only_visible_text(tmp_path):
    page = observe(tmp_path, HIDDEN)
    hidden = ["Masqué CSS", "Masqué attribut", "Masqué ARIA", "Masqué visibilité", "Masqué opacité"]
    assert missing(page, expect=[*hidden, "Visible ici"]) == hidden


@pytest.mark.parametrize(
    "panel",
    [
        "",  # the panel never opened
        '<aside aria-label="Panneau client" style="display:none"><p>Catégories</p></aside>',
        '<aside aria-label="Panneau client"><p>Coordonnées</p></aside>',
    ],
)
def test_text_elsewhere_on_the_page_does_not_satisfy_a_scoped_expectation(tmp_path, panel):
    page = observe(tmp_path, DROPDOWN + panel)
    assert missing(page, expect=["Catégories"]) == []  # unscoped: today's false pass, unchanged
    assert missing(page, expect_in=[("Panneau client", "Catégories")]) == [
        {"region": "Panneau client", "text": "Catégories"}
    ]


@pytest.mark.parametrize(
    "panel, region",
    [
        ('<aside aria-label="Panneau client"><p>Catégories</p></aside>', "Panneau client"),
        ('<span id="t">Panneau  client</span><section aria-labelledby="t"><p>Catégories</p></section>',
         "Panneau client"),
        ('<div role="dialog"><h2>Fiche du client</h2><p>Catégories</p></div>', "Fiche du client"),
        ('<dialog open><h2 hidden>Caché</h2><h3>Fiche</h3><p>Catégories</p></dialog>', "Fiche"),
    ],
)
def test_text_inside_the_named_region_satisfies_it(tmp_path, panel, region):
    page = observe(tmp_path, DROPDOWN + panel)
    assert missing(page, expect_in=[(region, "Catégories")]) == []
    assert missing(page, expect_in=[(region, "Tags")]) == [{"region": region, "text": "Tags"}]


def test_a_region_is_matched_by_its_whole_name_only(tmp_path):
    page = observe(tmp_path, '<aside aria-label="Panneau client"><p>Catégories</p></aside>')
    assert missing(page, expect_in=[("Panneau", "Catégories")]) == [{"region": "Panneau", "text": "Catégories"}]


class FakeBrowser:
    """Serves one real Chrome snapshot; stands in for the CDP transport only."""

    page = None

    def __init__(self, _url):
        pass

    def observe(self, screenshot=True):
        return {**self.page, "screenshot": base64.b64encode(b"").decode()} if screenshot else dict(self.page)

    def fresh(self, _page):
        return True

    def settle(self):
        return True

    def drain(self):
        return [], []

    def close(self):
        pass


@pytest.mark.parametrize(
    "panel, code, missing",
    [
        ("", 4, [{"region": "Panneau client", "text": "Catégories"}]),
        ('<aside aria-label="Panneau client"><p>Catégories</p></aside>', 0, []),
    ],
)
def test_navigate_exits_4_and_reports_a_scoped_miss_distinctly(tmp_path, monkeypatch, panel, code, missing):
    page = observe(tmp_path, DROPDOWN + panel)
    FakeBrowser.page = {**page, "fingerprint": fingerprint(page)}
    spec = importlib.util.spec_from_file_location("navigate", Path(__file__).parents[1] / "navigate.py")
    navigate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(navigate)
    monkeypatch.setattr(navigate, "ensure_chrome", lambda *_: (None, None))
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    done = {"choice": "DONE", "operation": "DONE", "target": None, "confidence": 1.0,
            "probabilities": {"DONE": 1.0}, "latency_ms": 1, "usage": {}}
    monkeypatch.setattr(loop, "choose", lambda *_: dict(done))  # no model call
    out = tmp_path / "run"
    monkeypatch.setattr("sys.argv", ["navigate.py", "--url", "file:///x", "--goal", "Ouvrir le panneau", "--out",
                                     str(out), "--quiet", "--expect", "Catégories",
                                     "--expect-in", "Panneau client", "Catégories"])
    assert navigate.main() == code
    summary = json.loads((out / "summary.json").read_text())
    assert summary["expect"] == ["Catégories"]
    assert summary["expect_in"] == [{"region": "Panneau client", "text": "Catégories"}]
    assert summary["missing_expect"] == missing  # the unscoped "Catégories" is satisfied by the dropdown
