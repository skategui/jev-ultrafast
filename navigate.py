#!/usr/bin/env python3
"""Jev navigation driver with a structured action log.

Runs ONE bounded navigation goal against a page and writes an auditable trace:

    <out>/trace.jsonl    one JSON object per EXECUTED action (not per decision)
    <out>/summary.json   status, counters, final url/title, the screenshots' names
    <out>/console.jsonl  console calls, uncaught exceptions and browser log entries
    <out>/network.jsonl  one line per finished document/fetch/XHR (and failed asset): method, url
                         without query, status, GraphQL operation, error codes. Never bodies.
    <out>/<ms>.jpg       the frames Jev records when `record_dir` is set

Split of duties: Jev decides *which operation on which element* and acts; this script
only OBSERVES and LOGS. It never rewrites the goal, never retries by itself, and never
turns `status=done` into a verdict — a `DONE` from Jev is a claim about the navigation,
not a proof that an acceptance criterion holds. The verifier reads the trace and decides
that separately.

Chrome: if `BU_CDP_URL`/`BU_CDP_WS` is already set, that endpoint is used and left alone.
Otherwise a Chrome already listening on `--chrome-port` is reused and ALSO left alone; if
none answers, this script launches its own (dedicated profile) and kills it on exit. It
never kills a Chrome it did not start.

Exit codes: 0 = done, 2 = blocked, 3 = error/exception, 4 = done claimed but an --expect text is missing.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

from jev_ultrafast import Agent
from jev_ultrafast.agent import STOPPED

PAGE_KEYS = ("url", "title", "fingerprint")


def cdp_alive(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_chrome() -> str | None:
    """First Chrome/Chromium that exists: explicit env, system install, agent-browser bundle."""
    env = os.environ.get("BH_CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if os.path.exists(path):
            return path
    bundled = sorted(glob.glob(os.path.expanduser(
        "~/.agent-browser/browsers/*/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing")))
    return bundled[-1] if bundled else None


def ensure_chrome(port: int, profile: str, chrome: str | None, keep: bool):
    """Return (cdp_url, proc). `proc` is not None only when we launched it, so only then do we kill it."""
    if os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS"):
        return None, None  # caller's endpoint — not ours to manage
    if cdp_alive(port):
        url = f"http://127.0.0.1:{port}"
        os.environ["BU_CDP_URL"] = url
        return url, None  # someone else's Chrome — reuse, never kill
    binary = chrome or find_chrome()
    if not binary:
        raise RuntimeError("no Chrome found: pass --chrome, set BH_CHROME_PATH, or set BU_CDP_URL")
    pathlib.Path(profile).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [binary, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--headless=new",
         "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
         "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if cdp_alive(port):
            break
        time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError(f"Chrome on :{port} did not answer CDP within 10s")
    url = f"http://127.0.0.1:{port}"
    os.environ["BU_CDP_URL"] = url
    return url, (None if keep else proc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="starting URL")
    parser.add_argument("--goal", required=True, help="natural-language goal, one bounded outcome")
    parser.add_argument("--out", required=True, help="trace directory (created if missing)")
    parser.add_argument("--chrome-port", type=int, default=9333)
    parser.add_argument("--chrome-profile", default="/tmp/jev-chrome-profile")
    parser.add_argument("--chrome", help="Chrome binary, if auto-discovery picks the wrong one")
    parser.add_argument("--keep-chrome", action="store_true",
                        help="leave our own Chrome running (interactive debugging)")
    parser.add_argument("--quiet", action="store_true", help="no per-step stdout")
    parser.add_argument("--secret-env", metavar="VAR",
                        help="env var holding the password; typed by code on the start origin only, "
                             "never sent to a model nor written to the trace")
    parser.add_argument("--expect", action="append", default=[], metavar="TEXT",
                        help="text that must be visible for DONE to count (repeatable); else status=unverified")
    args = parser.parse_args()

    out = pathlib.Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    trace = out / "trace.jsonl"
    trace.write_text("")  # a run owns its trace; never append to a previous one

    started = time.time()
    logged = 0
    page: dict = {}
    final: dict = {}
    status = "error"
    error: str | None = None
    cdp_url: str | None = None
    chrome_proc = None
    browser = None
    console_entries: list = []
    network_entries: list = []

    try:
        cdp_url, chrome_proc = ensure_chrome(args.chrome_port, args.chrome_profile,
                                             args.chrome, args.keep_chrome)
        secret = None
        if args.secret_env:
            secret = os.environ.get(args.secret_env)
            if not secret:
                raise ValueError(f"--secret-env {args.secret_env}: variable is unset or empty")
        with Agent(args.url, args.goal, record_dir=out, secret=secret, expect=args.expect) as agent:
            browser = agent.browser
            while agent.state["status"] not in STOPPED:
                state = agent.command("tick")
                console, network = agent.browser.drain()
                console_entries += console
                network_entries += network
                page = state.get("page") or page
                history = state.get("history") or []
                for step in history[logged:]:
                    with trace.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(step, ensure_ascii=False) + "\n")
                    if not args.quiet:
                        print(
                            "  {step:>2}. {kind:<9} {action:<56} p={prob:.2f} changed={changed}".format(
                                step=step.get("step", "?"),
                                kind=str(step.get("kind"))[:9],
                                action=str(step.get("action"))[:56],
                                prob=float(step.get("probability") or 0.0),
                                changed=step.get("page_changed"),
                            )
                        )
                logged = len(history)
            status = agent.state["status"]
            page = agent.state.get("page") or page
            final = agent.snapshot()
    except Exception as exc:  # noqa: BLE001 - the trace must survive a crash
        error = f"{type(exc).__name__}: {exc}"
        if not args.quiet:
            print(f"  ! {error}", file=sys.stderr)
    finally:
        # The daemon buffers events; this last drain also covers a crash mid-loop.
        if browser is not None:
            try:
                console, network = browser.drain()
                console_entries += console
                network_entries += network
            except Exception:
                pass
        if chrome_proc is not None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()

    console_log = out / "console.jsonl"
    with console_log.open("w", encoding="utf-8") as handle:
        for entry in console_entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    network_log = out / "network.jsonl"
    with network_log.open("w", encoding="utf-8") as handle:
        for entry in network_entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    summary = {
        "status": status,
        "error": error,
        "console_errors": sum(1 for e in console_entries if e.get("level") == "error"),
        "console_log": str(console_log),
        "network_requests": len(network_entries),
        "network_errors": sum(
            1 for e in network_entries
            if e.get("failed") or (e.get("status") or 0) >= 400 or e.get("code") or e.get("graphql_errors")
        ),
        "network_log": str(network_log),
        "goal": args.goal,
        "start_url": args.url,
        "cdp_url": cdp_url,
        "chrome_launched_by_us": chrome_proc is not None or (cdp_url is not None and args.keep_chrome),
        "chrome_left_running": bool(args.keep_chrome and cdp_url),
        "steps_executed": logged,
        "expect": args.expect,
        "missing_expect": final.get("missing_expect", []),
        "blocked_reason": final.get("blocked_reason"),
        "wall_ms": round((time.time() - started) * 1000),
        "final_page": {k: page.get(k) for k in PAGE_KEYS if k in page},
        "text_calls": [
            {"field": c.get("field"), "value": c.get("value"), "model": c.get("model")}
            for c in (final.get("text_calls") or [])
        ],
        "screenshots": sorted(p.name for p in out.glob("*.jpg")),
        "trace": str(trace),
        "note": (
            "status=done means Jev believes the goal was reached. It is NOT proof that an "
            "acceptance criterion holds: read trace.jsonl and the screenshots, then decide."
        ),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return {"done": 0, "blocked": 2, "unverified": 4}.get(status, 3)


if __name__ == "__main__":
    raise SystemExit(main())
