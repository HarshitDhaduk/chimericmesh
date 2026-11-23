"""MCP wrappers of the agent loop (task 8.1): thin, parameters unchanged, one route each, nothing clamped."""
from __future__ import annotations
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import asyncio
import json
import os
import tempfile
import httpx
import pytest
from chimericmesh import mcp_server
from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
class Borrowed:
    """Wrap a shared client so ``async with`` in the tool does not close it."""
    def __init__(self, client):
        self.client = client
    async def __aenter__(self):
        return self.client
    async def __aexit__(self, *exc):
        return False
@pytest.fixture()
def home(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(tmp.name, "home"))
    yield tmp.name
    tmp.cleanup()
def recording_client(monkeypatch, handler=None):
    """A client over ``httpx.MockTransport`` that records every request."""
    seen: list[httpx.Request] = []
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json={"ok": True})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://hub", timeout=10.0)
    monkeypatch.setattr(mcp_server, "_client_factory", lambda: Borrowed(client))
    return seen
def run(coro):
    return asyncio.run(coro)
NEW_TOOLS = {"list_services", "get_service", "service_events", "explain_log_window", "find_code_for_service",
             "verify_proposal", "wait_for_work"}
def test_each_new_tool_calls_exactly_its_route_with_unchanged_parameters(monkeypatch):
    seen = recording_client(monkeypatch)
    async def go():
        await mcp_server.list_services()
        await mcp_server.get_service("svc")
        await mcp_server.service_events("svc", 9999, 5, 2)
        await mcp_server.explain_log_window("svc", 10_000)
        await mcp_server.find_code_for_service("svc", "disk full")
        await mcp_server.verify_proposal("p1", ["/a/b.py", "rel.py"])
        await mcp_server.wait_for_work(99, "svc", ["i1", "p2"])
    run(go())
    got = [(r.method, r.url.path, dict(r.url.params), r.content) for r in seen]
    assert [(m, p) for m, p, _, _ in got] == [
        ("GET", "/api/services"), ("GET", "/api/services/svc"), ("GET", "/api/services/svc/events"),
        ("GET", "/api/services/svc/digest"), ("GET", "/api/services/svc/code-search"),
        ("POST", "/api/proposals/p1/verify"), ("GET", "/api/watch/next")]
    assert got[2][2] == {"limit": "9999", "after_id": "5", "min_severity": "2"}     # nothing clamped
    assert got[3][2] == {"minutes": "10000"}
    assert got[4][2] == {"query": "disk full"}
    assert json.loads(got[5][3]) == {"files": ["/a/b.py", "rel.py"]}
    assert got[6][2]["timeout_seconds"] == "99" and got[6][2]["service_id"] == "svc"
def test_none_parameters_are_not_sent(monkeypatch):
    seen = recording_client(monkeypatch)
    async def go():
        await mcp_server.service_events("svc")
        await mcp_server.wait_for_work()
    run(go())
    assert dict(seen[0].url.params) == {"limit": "25"}
    assert dict(seen[1].url.params) == {"timeout_seconds": "25"}
def test_exclude_ids_is_sent_as_repeated_query_parameters(monkeypatch):
    seen = recording_client(monkeypatch)
    run(mcp_server.wait_for_work(25, None, ["i1", "i2", "p3"]))
    assert seen[0].url.params.get_list("exclude_ids") == ["i1", "i2", "p3"]
    assert seen[0].url.query.count(b"exclude_ids=") == 3
def test_wait_for_work_has_a_35_second_read_timeout_and_other_tools_keep_the_factory_default(monkeypatch):
    seen = recording_client(monkeypatch)
    async def go():
        await mcp_server.wait_for_work()
        await mcp_server.list_services()
        await mcp_server.service_events("svc")
        await mcp_server.get_proposal("p1")
    run(go())
    assert seen[0].extensions["timeout"]["read"] == 35.0
    for request in seen[1:]:
        assert request.extensions["timeout"]["read"] == 10.0
def test_the_default_client_timeout_stays_ten_seconds():
    client = mcp_server._client_factory()
    assert client.timeout.read == 10.0 and client.timeout.connect == 10.0
def test_propose_remediation_sends_only_the_keys_that_are_not_none(monkeypatch):
    seen = recording_client(monkeypatch)
    async def go():
        await mcp_server.propose_remediation("i", "a", "d")
        await mcp_server.propose_remediation("i", "a", "d", ["/x.py"], 7, "run tests", "code_change")
        await mcp_server.complete_proposal("p1", "note")
        await mcp_server.complete_proposal("p1", "note", ["/x.py"], 3, "ok")
    run(go())
    bodies = [json.loads(r.content) for r in seen]
    assert bodies[0] == {"incident_id": "i", "action": "a", "details": "d"}
    assert bodies[1] == {"incident_id": "i", "action": "a", "details": "d", "files": ["/x.py"],
                         "estimated_lines": 7, "verification": "run tests", "kind": "code_change"}
    assert bodies[2] == {"note": "note"}
    assert bodies[3] == {"note": "note", "files_changed": ["/x.py"], "lines_changed": 3, "verification_result": "ok"}
def test_error_bodies_keep_their_extra_keys(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/409"):
            return httpx.Response(409, json={"detail": "ActiveProposalExists", "proposal_id": "p7"})
        if request.url.path.endswith("/422"):
            return httpx.Response(422, json={"detail": "bad value", "field": "files"})
        if request.url.path.endswith("/text"):
            return httpx.Response(502, text="Bad Gateway")
        if request.url.path.endswith("/list"):
            return httpx.Response(422, json={"detail": [{"loc": ["query"], "msg": "x"}]})
        return httpx.Response(404, json={"detail": "nope", "error": "spoof", "extra": 1})
    recording_client(monkeypatch, handler)
    async def go():
        return [await mcp_server._call("GET", f"/api/x/{leaf}") for leaf in ("409", "422", "text", "list", "other")]
    out = run(go())
    assert out[0] == {"error": 409, "detail": "ActiveProposalExists", "proposal_id": "p7"}
    assert out[1] == {"error": 422, "detail": "bad value", "field": "files"}
    assert out[2] == {"error": 502, "detail": "Bad Gateway"}
    assert out[3]["error"] == 422 and isinstance(out[3]["detail"], list)
    assert out[4]["error"] == 404 and out[4]["detail"] == "nope" and out[4]["extra"] == 1   # the status wins
def test_instructions_keep_their_words_and_mention_wait_for_work():
    text = mcp_server.mcp.instructions
    assert "propose_remediation" in text and "approved" in text and "wait_for_work" in text
def test_new_tools_are_registered_and_the_old_ones_remain():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert NEW_TOOLS <= names
    assert {"list_incidents", "get_incident", "find_code_for_incident", "recent_events", "announce_to_irc",
            "resolve_incident", "propose_remediation", "get_proposal", "list_proposals", "complete_proposal"} <= names
def test_path_parameters_cannot_redirect_the_call(monkeypatch):
    seen = recording_client(monkeypatch)
    async def go():
        await mcp_server.get_service("x/../../rules")
        await mcp_server.service_events("a?b=1#c")
        await mcp_server.verify_proposal("p1/../../rules", ["/a"])
    run(go())
    for request in seen:
        assert request.url.path.startswith(("/api/services/", "/api/proposals/"))
        assert "rules" not in request.url.path.split("/")[-1:] or "%2F" in str(request.url)
    assert seen[0].url.raw_path.startswith(b"/api/services/x%2F..%2F..%2Frules")
    assert seen[1].url.path.endswith("/events") and "b=1" not in seen[1].url.query.decode()
    assert seen[2].url.path.endswith("/verify") and "%2F" in str(seen[2].url)
def test_tools_work_end_to_end_against_the_app(home, monkeypatch, tmp_path):
    async def go():
        hub = Hub()
        app = create_app(hub, start_listeners=False)
        code = tmp_path / "code"
        code.mkdir()
        (code / "app.py").write_text("raise OSError('disk exploded')\n", encoding="utf-8")
        await app.state.services.create_service(
            ServiceFields(id="svc", name="svc", hosts=("web01",), code_folder=str(code)), actor="t")
        for n in range(4):
            await hub.ingest(Event(id=0, ts=hub.now(), source="syslog", host="web01", severity=2,
                                   message=f"disk exploded {n}", app="app"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            monkeypatch.setattr(mcp_server, "_client_factory", lambda: Borrowed(c))
            assert "svc" in [s["id"] for s in await mcp_server.list_services()]
            assert (await mcp_server.get_service("svc"))["code_folder"] == str(code)
            assert len((await mcp_server.service_events("svc", 2))["events"]) == 2
            assert (await mcp_server.explain_log_window("svc", 5))["total_events"] == 4
            assert (await mcp_server.find_code_for_service("svc", "disk exploded"))["hits"][0]["file"] == "app.py"
            assert (await mcp_server.get_service("nope"))["error"] == 404
            work = await mcp_server.wait_for_work(1)
            assert work["status"] == "work" and work["incidents"]
            iid = work["incidents"][0]["id"]
            assert (await mcp_server.wait_for_work(1, "svc", [iid]))["status"] == "timeout"
            prop = await mcp_server.propose_remediation(iid, "edit app.py", "d", ["app.py"], 3, "pytest")
            assert prop.get("status") == "pending", prop
            assert (await mcp_server.verify_proposal(prop["id"], [str(code / "app.py")]))["ok"] is False
    run(go())
