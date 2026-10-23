"""network.jsonl contracts: what a verifier reads about the page's requests, and what it never gets."""

import base64
import json

from jev_ultrafast import browser as harness
from jev_ultrafast.browser import Browser, error_codes, graphql_operation


def event(method, **params):
    return {"session_id": "s1", "method": method, "params": params}


def sent(rid, url, kind="Fetch", method="POST", body=None):
    request = {"url": url, "method": method}
    if body is not None:
        request["postData"] = json.dumps(body)
    return event("Network.requestWillBeSent", requestId=rid, type=kind, request=request)


def answered(rid, status, mime="application/json"):
    return event("Network.responseReceived", requestId=rid, response={"status": status, "mimeType": mime})


def finished(rid):
    return event("Network.loadingFinished", requestId=rid)


def drained(monkeypatch, events, bodies=None):
    page = Browser.__new__(Browser)
    page.session, page.requests = "s1", {}
    calls = []

    def call(method, **params):
        calls.append(params["requestId"])
        text = (bodies or {})[params["requestId"]]
        return {"body": base64.b64encode(text.encode()).decode(), "base64Encoded": True}

    page.call = call
    monkeypatch.setattr(harness, "drain_events", lambda: events)
    console, network = page.drain()
    return console, network, calls


def test_graphql_operation_names_single_and_batched_requests():
    assert graphql_operation(json.dumps({"operationName": "DoctorPatientFile", "query": "q"})) == "DoctorPatientFile"
    assert graphql_operation(json.dumps([{"operationName": "A"}, {"operationName": "B"}])) == "A,B"
    assert graphql_operation("email=a@b.test") is None
    assert graphql_operation(None) is None


def test_error_codes_keep_machine_codes_only():
    assert error_codes('{"code":"RATE_LIMITED"}') == {"code": "RATE_LIMITED"}
    assert error_codes('{"code":"Incorrect password for jane@x.test"}') == {}
    gql = {
        "data": {"patient": {"weight": 74}},
        "errors": [{"message": "id 123 missing", "extensions": {"code": "NOT_FOUND"}}],
    }
    assert error_codes(json.dumps(gql)) == {"graphql_errors": ["NOT_FOUND"]}
    assert error_codes(json.dumps({"data": {"x": 1}})) == {}
    assert error_codes("<html>") == {}


def test_refused_login_carries_the_code_the_ui_may_hide(monkeypatch):
    events = [
        sent("r1", "http://localhost:3001/api/auth/login?next=/x", body={"email": "a@b.test"}),
        answered("r1", 400),
        finished("r1"),
    ]
    _, network, _ = drained(monkeypatch, events, {"r1": '{"code":"RATE_LIMITED"}'})
    assert network == [
        {
            "type": "Fetch",
            "method": "POST",
            "url": "http://localhost:3001/api/auth/login",
            "status": 400,
            "code": "RATE_LIMITED",
        }
    ]


def test_graphql_success_logs_operation_without_data(monkeypatch):
    body = {"operationName": "DoctorPatientFile", "variables": {"id": "p1"}}
    events = [sent("r1", "http://localhost:3001/api/graphql", body=body), answered("r1", 200), finished("r1")]
    _, network, _ = drained(monkeypatch, events, {"r1": '{"data":{"patient":{"lastName":"Moreau"}}}'})
    assert network == [
        {
            "type": "Fetch",
            "method": "POST",
            "url": "http://localhost:3001/api/graphql",
            "status": 200,
            "operation": "DoctorPatientFile",
        }
    ]
    assert "Moreau" not in json.dumps(network) and "p1" not in json.dumps(network)


def test_assets_are_logged_only_when_they_fail(monkeypatch):
    events = [
        sent("ok", "http://h/app.css", kind="Stylesheet", method="GET"),
        answered("ok", 200, mime="text/css"),
        finished("ok"),
        sent("gone", "http://h/favicon.ico", kind="Other", method="GET"),
        answered("gone", 404, mime="text/html"),
        finished("gone"),
        sent("dns", "https://tracker.test/e?u=1", kind="Ping", method="GET"),
        event("Network.loadingFailed", requestId="dns", errorText="net::ERR_NAME_NOT_RESOLVED"),
    ]
    _, network, calls = drained(monkeypatch, events)
    assert [(e["url"], e.get("status"), e.get("failed")) for e in network] == [
        ("http://h/favicon.ico", 404, None),
        ("https://tracker.test/e", None, "net::ERR_NAME_NOT_RESOLVED"),
    ]
    assert calls == []  # no body read for assets


def test_a_request_split_across_drains_is_logged_once(monkeypatch):
    page = Browser.__new__(Browser)
    page.session, page.requests = "s1", {}
    page.call = lambda method, **params: {"body": '{"data":{}}', "base64Encoded": False}
    monkeypatch.setattr(
        harness, "drain_events", lambda: [sent("r1", "http://h/api/graphql", body={"operationName": "Me"})]
    )
    assert page.drain()[1] == []
    monkeypatch.setattr(harness, "drain_events", lambda: [answered("r1", 200), finished("r1")])
    assert page.drain()[1] == [
        {"type": "Fetch", "method": "POST", "url": "http://h/api/graphql", "status": 200, "operation": "Me"}
    ]


def test_console_entries_still_come_out_of_the_same_drain(monkeypatch):
    events = [event("Runtime.exceptionThrown", exceptionDetails={"exception": {"description": "TypeError: x"}})]
    console, network, _ = drained(monkeypatch, events)
    assert [c["text"] for c in console] == ["TypeError: x"] and network == []


def test_other_sessions_are_ignored(monkeypatch):
    other = sent("r9", "http://h/api/graphql", body={"operationName": "X"})
    other["session_id"] = "s2"
    assert drained(monkeypatch, [other])[1] == []
