"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp, drain_events

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

# Start time of each fetch/XHR in flight and when the last one ended, so a page still loading its data
# never looks settled while background traffic (long polls, retries, refetches) can be told apart by age.
NET_PROBE = """(() => { if (window.__jevNet) return; const n = window.__jevNet = {open: new Set(), end: -1e9};
  const start = () => { const r = {t: performance.now()}; n.open.add(r);
    return () => { n.open.delete(r); n.end = performance.now(); }; };
  const f = window.fetch;
  if (f) window.fetch = function (...a) { const done = start(); return f.apply(this, a).finally(done); };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...a) {
    this.addEventListener('loadend', start(), {once: true}); return send.apply(this, a);
  };
})()"""
# A request older than this is background traffic, not the data the page is about to show.
BACKGROUND_S = 1.2
SETTLE_STATE = f"""(() => {{ const n = window.__jevNet, now = performance.now(), marker = {MARKER};
  return n ? [marker, [...n.open].filter(r => now - r.t < {BACKGROUND_S * 1000}).length, now - n.end]
    : [marker, 0, 1e9];
}})()"""


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        # Runtime -> consoleAPICalled + exceptionThrown; Log -> network/CSP/deprecation entries.
        self.call("Runtime.enable")
        self.call("Log.enable")
        # Network -> network.jsonl: status, GraphQL operation and error codes; never bodies or headers.
        self.requests = {}
        self.call("Network.enable")
        self.call("Page.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", source=NET_PROBE)
        # next dev compiles a route on its first hit; Page.navigate only answers once the server
        # responds, well past browser_harness's 5 s IPC default.
        cdp("Page.navigate", session_id=self.session, _response_timeout=60, url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def settle(self, quiet=0.3, timeout=5.0):
        """True once the page stayed identical for `quiet` s, no fetch/XHR younger than BACKGROUND_S is in
        flight and none ended in the last `quiet` s; False at `timeout`.

        shortcut: a genuine data request slower than BACKGROUND_S counts as background, so settle() (and DONE)
        can pass while its spinner shows; raise BACKGROUND_S if a slow API needs it.
        """
        deadline = time.monotonic() + timeout
        last, since = object(), time.monotonic()
        while time.monotonic() < deadline:
            try:
                marker, young, idle_ms = self.evaluate(SETTLE_STATE)
            except StalePage:
                marker = young = idle_ms = None
            now = time.monotonic()
            # Only the page itself restarts the quiet window: a request ageing into background must not.
            if marker != last:
                last, since = marker, now
            # The quiet after the last response covers the app's render, which runs after the probe's finally.
            elif marker is not None and not young and idle_ms >= quiet * 1000 and now - since >= quiet:
                return True
            time.sleep(0.05)
        return False

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def drain(self):
        """Console and network entries captured on this session since the previous drain."""
        console, network = [], []
        for event in drain_events():
            if event.get("session_id") != self.session:
                continue
            method = event.get("method")
            if method in CONSOLE_METHODS and (entry := console_entry(event)) is not None:
                console.append(entry)
            elif method in NETWORK_METHODS and (entry := self.network_entry(method, event.get("params", {}))):
                network.append(entry)
        return console, network

    def network_entry(self, method, params):
        """Fold one request's events into a network.jsonl line, returned once it has finished."""
        request_id = params.get("requestId")
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            entry = {
                "type": params.get("type"),
                "method": request.get("method"),
                # The query string can carry tokens or ids: scheme, host and path only.
                "url": request.get("url", "").split("#")[0].split("?")[0],
            }
            if operation := graphql_operation(request.get("postData")):
                entry["operation"] = operation
            self.requests[request_id] = entry
            return None
        entry = self.requests.get(request_id)
        if entry is None:
            return None
        if method == "Network.responseReceived":
            response = params.get("response", {})
            entry["status"] = response.get("status")
            entry["_json"] = "json" in (response.get("mimeType") or "")
            return None
        del self.requests[request_id]
        is_json = entry.pop("_json", False)
        status = entry.get("status") or 0
        if method == "Network.loadingFailed":
            entry["failed"] = params.get("errorText")
        elif is_json and entry["type"] in ("Fetch", "XHR") and (status >= 400 or "operation" in entry):
            entry.update(error_codes(self.response_body(request_id)))
        # shortcut: requests still open at the last drain (sockets, long polls) are dropped.
        return entry if entry["type"] in DATA_TYPES or "failed" in entry or status >= 400 else None

    def response_body(self, request_id):
        try:
            body = self.call("Network.getResponseBody", requestId=request_id)
        except Exception:  # noqa: BLE001 - evicted after a navigation: the line keeps its status
            return None
        text = body.get("body") or ""
        return base64.b64decode(text).decode("utf-8", "replace") if body.get("base64Encoded") else text

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


CONSOLE_METHODS = {"Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"}
NETWORK_METHODS = {
    "Network.requestWillBeSent",
    "Network.responseReceived",
    "Network.loadingFinished",
    "Network.loadingFailed",
}
# The page itself and its data calls; scripts, styles and images only when they fail.
DATA_TYPES = {"Document", "Fetch", "XHR"}
CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def graphql_operation(post_data):
    """The GraphQL operation name(s) a request body names, or None."""
    try:
        payload = json.loads(post_data) if post_data else None
    except ValueError:
        return None
    batch = payload if isinstance(payload, list) else [payload]
    names = [p["operationName"] for p in batch if isinstance(p, dict) and isinstance(p.get("operationName"), str)]
    return ",".join(names) or None


def error_codes(text):
    """Machine codes of a JSON answer (`code`, GraphQL `errors[].extensions.code`), never its data or messages."""
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    codes = {}
    if isinstance(payload.get("code"), str) and CODE.match(payload["code"]):
        codes["code"] = payload["code"]
    if isinstance(payload.get("errors"), list):
        found = [((e.get("extensions") or {}).get("code") if isinstance(e, dict) else None) for e in payload["errors"]]
        codes["graphql_errors"] = [c if isinstance(c, str) and CODE.match(c) else "UNKNOWN" for c in found]
    return codes


def console_entry(event):
    """Normalize a CDP event into one console.jsonl line, or None if it is not one."""
    method, params = event.get("method"), event.get("params", {})
    if method == "Runtime.consoleAPICalled":
        args = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
        frames = (params.get("stackTrace") or {}).get("callFrames") or [{}]
        return {
            "kind": "console",
            "level": params.get("type"),
            "text": " ".join(args).strip(),
            "url": frames[0].get("url"),
            "line": frames[0].get("lineNumber"),
        }
    if method == "Runtime.exceptionThrown":
        details = params.get("exceptionDetails", {})
        exception = details.get("exception") or {}
        return {
            "kind": "exception",
            "level": "error",
            "text": exception.get("description") or details.get("text"),
            "url": details.get("url"),
            "line": details.get("lineNumber"),
        }
    if method == "Log.entryAdded":
        entry = params.get("entry", {})
        return {
            "kind": "log",
            "level": entry.get("level"),
            "source": entry.get("source"),
            "text": entry.get("text"),
            "url": entry.get("url"),
            "line": entry.get("lineNumber"),
        }
    return None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
