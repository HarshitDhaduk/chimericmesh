"""REST routes of the agent loop (task 7.2): a small FastAPI app over a real AgentService, driven by httpx."""
from __future__ import annotations
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import ast
import asyncio
import os
import re
import time
from pathlib import Path
import httpx
import pytest
from fastapi import FastAPI
from agent_helpers import make_agent
from chimericmesh.agent.routes import build_router
from chimericmesh.agent.watch import WatchQueue
from chimericmesh.events import Event
def build(rig, *, wait_max: float = 1, tick: float = 0.05) -> FastAPI:
    rig.agent.watch = WatchQueue(rig.agent._ports, wait_max=wait_max, tick=tick)
    app = FastAPI()
    app.include_router(build_router(rig.agent))
    return app
def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
@pytest.fixture
def rig():
    r = make_agent("bare")
    yield r
    r.close()
def run(rig, scenario, **kw):
    async def go():
        await rig.add_service()
        app = build(rig, **kw)
        async with client(app) as c:
            return await scenario(c, app)
    return asyncio.run(go())
async def ingest_many(rig, n: int, *, message: str = "boom", severity: int = 3, service: bool = True) -> None:
    for k in range(n):
        await rig.hub.ingest(Event(id=0, ts=rig.hub.now(), source="syslog", host="web01", severity=severity,
                                   message=f"{message} {k}", app="app"))
def test_every_route_returns_its_success_shape(rig):
    async def scenario(c, app):
        write = os.path.join(rig.folder, "app.py")
        with open(write, "w", encoding="utf-8") as fh:
            fh.write("raise OSError('boom happens')\n")
        await ingest_many(rig, 4)
        r = await c.get("/api/services/api-gw/events")
        assert r.status_code == 200 and set(r.json()) == {"service_id", "events", "next_after_id", "truncated"}
        r = await c.get("/api/services/api-gw/digest")
        assert r.status_code == 200 and r.json()["minutes"] == 15 and set(r.json()["severity_counts"]) == set("01234567")
        r = await c.get("/api/services/api-gw/code-search", params={"query": "boom"})
        assert r.status_code == 200 and r.json()["hits"][0]["file"] == "app.py"
        iid = rig.hub.incidents()[0].id
        prop = await rig.user_approved(iid, ["app.py"])
        r = await c.post(f"/api/proposals/{prop.id}/verify", json={"files": [write]})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert set(r.json()) == {"ok", "reasons", "status", "approval", "declared_files"}
        r = await c.get("/api/watch/next", params={"timeout_seconds": 1})
        assert r.status_code == 200 and set(r.json()) == {"status", "incidents", "approved_proposals"}
        r = await c.get("/api/services/api-gw/permissions-snippet")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert "Mode: AUTONOMOUS" in r.text
    run(rig, scenario)
def test_numbers_are_clamped_and_the_effective_value_shows(rig):
    async def scenario(c, app):
        await ingest_many(rig, 150)
        counts = {}
        for limit in (0, -5, 10_000, 7):
            body = (await c.get("/api/services/api-gw/events", params={"limit": limit})).json()
            counts[limit] = len(body["events"])
        assert counts == {0: 1, -5: 1, 10_000: 100, 7: 7}
        first = (await c.get("/api/services/api-gw/events", params={"after_id": -3, "limit": 3})).json()
        assert [e["id"] for e in first["events"]] == [e["id"] for e in
               (await c.get("/api/services/api-gw/events", params={"after_id": 0, "limit": 3})).json()["events"]]
        sev = (await c.get("/api/services/api-gw/events", params={"min_severity": 99, "limit": 100})).json()
        assert len(sev["events"]) == 100                  # 99 clamps to 7: every event is at least that severe
        none = (await c.get("/api/services/api-gw/events", params={"min_severity": -4, "limit": 100})).json()
        assert none["events"] == []                       # -4 clamps to 0: nothing at severity 0
        for minutes, shown in ((0, 1), (10_000, 240), (30, 30)):
            body = (await c.get("/api/services/api-gw/digest", params={"minutes": minutes})).json()
            assert body["minutes"] == shown
    run(rig, scenario)
def test_timeout_seconds_is_clamped_to_one_and_the_wait_ceiling(rig):
    async def scenario(c, app):
        for asked, expected in ((0, 1), (99, 1.0)):
            start = time.monotonic()
            r = await c.get("/api/watch/next", params={"timeout_seconds": asked})
            took = time.monotonic() - start
            assert r.json()["status"] == "timeout"
            assert expected - 0.05 <= took <= expected + 1.0, (asked, took)
    run(rig, scenario, wait_max=1)
def test_non_integers_give_422(rig):
    async def scenario(c, app):
        for path, params in (("/api/services/api-gw/events", {"limit": "x"}),
                             ("/api/services/api-gw/events", {"after_id": "1.5"}),
                             ("/api/services/api-gw/events", {"min_severity": "high"}),
                             ("/api/services/api-gw/digest", {"minutes": "x"}),
                             ("/api/watch/next", {"timeout_seconds": "x"})):
            assert (await c.get(path, params=params)).status_code == 422, (path, params)
    run(rig, scenario)
def test_code_search_query_validation(rig):
    async def scenario(c, app):
        for params in ({}, {"query": ""}, {"query": "x" * 101}, {"query": "a\x07b"}, {"query": "a\nb"}):
            r = await c.get("/api/services/api-gw/code-search", params=params)
            assert r.status_code == 422, params
            assert r.json()["field"] == "query"
        ok = await c.get("/api/services/api-gw/code-search", params={"query": "x" * 100})
        assert ok.status_code == 200
    run(rig, scenario)
def test_a_service_without_a_code_folder_gives_422(rig):
    async def scenario(c, app):
        await rig.add_service("nofolder", folder=None, hosts=("db01",))
        r = await c.get("/api/services/nofolder/code-search", params={"query": "x"})
        assert r.status_code == 422 and r.json()["detail"] == "service has no code folder"
        assert (await c.get("/api/services/nofolder/permissions-snippet")).status_code == 422
    run(rig, scenario)
def test_unassigned_and_unknown_ids_give_404_on_every_per_service_route(rig):
    async def scenario(c, app):
        for sid in ("unassigned", "nope"):
            for path, params in ((f"/api/services/{sid}/events", {}), (f"/api/services/{sid}/digest", {}),
                                 (f"/api/services/{sid}/code-search", {"query": "x"}),
                                 (f"/api/services/{sid}/permissions-snippet", {})):
                r = await c.get(path, params=params)
                assert r.status_code == 404 and r.json()["detail"] == "service not found", (path)
        r = await c.get("/api/watch/next", params={"service_id": "nope"})
        assert r.status_code == 404
        r = await c.get("/api/watch/next", params={"service_id": "unassigned", "timeout_seconds": 1})
        assert r.status_code == 200
        assert (await c.post("/api/proposals/p404/verify", json={"files": ["/a"]})).status_code == 404
    run(rig, scenario)
def test_exclude_ids_bounds(rig):
    async def scenario(c, app):
        many = [f"i{n}" for n in range(201)]
        assert (await c.get("/api/watch/next", params={"exclude_ids": many})).status_code == 422
        for bad in ("x" * 41, "has space", "a/b", "", "a\nb"):
            assert (await c.get("/api/watch/next", params={"exclude_ids": [bad]})).status_code == 422, bad
        ok = await c.get("/api/watch/next", params={"exclude_ids": [f"i{n}" for n in range(200)] + [], "timeout_seconds": 1})
        assert ok.status_code == 200
        ok = await c.get("/api/watch/next", params={"exclude_ids": ["x" * 40, "a.b:c-d_e"], "timeout_seconds": 1})
        assert ok.status_code == 200
    run(rig, scenario)
def test_exclude_ids_hide_the_incident(rig):
    async def scenario(c, app):
        await ingest_many(rig, 4, message="disk full on /var")
        first = (await c.get("/api/watch/next", params={"timeout_seconds": 1})).json()
        assert first["status"] == "work"
        iid = first["incidents"][0]["id"]
        second = (await c.get("/api/watch/next", params={"timeout_seconds": 1, "exclude_ids": [iid]})).json()
        assert second["status"] == "timeout"
    run(rig, scenario)
def test_a_fifth_waiter_gets_429_with_retry_after(rig):
    async def scenario(c, app):
        held = [asyncio.create_task(c.get("/api/watch/next", params={"timeout_seconds": 1})) for _ in range(4)]
        await asyncio.sleep(0.2)
        assert rig.agent.watch.waiting == 4
        fifth = await c.get("/api/watch/next", params={"timeout_seconds": 1})
        assert fifth.status_code == 429 and fifth.headers["retry-after"] == "5"
        for r in await asyncio.gather(*held):
            assert r.status_code == 200
    run(rig, scenario)
def test_verify_bounds(rig):
    async def scenario(c, app):
        iid = await rig.incident()
        target = os.path.join(rig.folder, "a.py")
        prop = await rig.user_approved(iid, ["a.py"])
        url = f"/api/proposals/{prop.id}/verify"
        assert (await c.post(url, json={"files": [f"/a/{n}.py" for n in range(201)]})).status_code == 422
        r = await c.post(url, json={"files": [f"/a/{n}.py" for n in range(21)]})
        assert r.status_code == 200 and r.json()["ok"] is False
        assert (await c.post(url, json={"files": ["/a" * 600]})).status_code == 422
        assert (await c.post(url, json={"files": [target], "extra": 1})).status_code == 422
        assert (await c.post(url, json={})).status_code == 422
        assert (await c.post(url, json={"files": [1]})).status_code == 422
        assert (await c.post(url, json={"files": []})).json()["ok"] is False
    run(rig, scenario)
def test_log_text_is_capped_and_clean_in_every_response(rig):
    async def scenario(c, app):
        dirty = "a\x00b\x1b[31m" + "Z" * 900
        for _ in range(4):
            await rig.hub.ingest(Event(id=0, ts=rig.hub.now(), source="syslog", host="web01", severity=2,
                                       message=dirty, app="app"))
        events = (await c.get("/api/services/api-gw/events")).json()["events"]
        assert events and all(len(e["message"]) <= 500 for e in events)
        digest = (await c.get("/api/services/api-gw/digest")).json()
        assert all(len(s["sample"]) <= 200 for s in digest["top_signatures"])
        work = (await c.get("/api/watch/next", params={"timeout_seconds": 1})).json()
        assert work["status"] == "work" and all(len(i["sample"]) <= 200 for i in work["incidents"])
        blob = (await c.get("/api/services/api-gw/events")).text + (await c.get("/api/services/api-gw/digest")).text
        assert "\\u0000" not in blob and "\\u001b" not in blob
    run(rig, scenario)
def test_a_real_client_disconnect_frees_the_slot_within_a_tick(rig):
    """Drive the ASGI app directly: Starlette's ``is_disconnected`` is the only thing that sees the client go."""
    async def go():
        await rig.add_service()
        app = build(rig, wait_max=30, tick=0.05)
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"", "more_body": False})
        sent: list[dict] = []
        async def receive():
            return await queue.get()
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": "/api/watch/next", "raw_path": b"/api/watch/next", "query_string": b"timeout_seconds=30",
                 "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.sleep(0.25)
        assert rig.agent.watch.waiting == 1 and not task.done()
        start = time.monotonic()
        await queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        assert time.monotonic() - start < 0.05 + 0.6
        assert rig.agent.watch.waiting == 0
    asyncio.run(go())
def test_routes_are_async_read_only_and_have_no_admin_dependency():
    path = Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "agent" / "routes.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods, handlers = [], 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and \
                        isinstance(deco.func.value, ast.Name) and deco.func.value.id == "router":
                    handlers += 1
                    methods.append(deco.func.attr)
                    assert isinstance(node, ast.AsyncFunctionDef), node.name
    assert handlers == 6 and sorted(methods) == ["get", "get", "get", "get", "get", "post"]
    assert "require_admin" not in source and "Depends" not in source
