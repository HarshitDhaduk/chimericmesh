"""Wiring of the agent loop into the app (task 7.3): routes, B6, shutdown, workspace root, golden outputs."""
from __future__ import annotations
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
import httpx
import pytest
from chimericmesh import __main__ as entry
from chimericmesh.api import build_registry, create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
from chimericmesh.store import Store
from route_owners import OWNERS
GOLDEN = Path(__file__).with_name("golden_agent_wiring.json")
@pytest.fixture()
def home(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(tmp.name, "home"))
    monkeypatch.delenv("CHIMERICMESH_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("CHIMERICMESH_CODE_ROOT", raising=False)
    yield tmp.name
    tmp.cleanup()
def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
def route_set(app) -> set[tuple[str, str]]:
    return {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) or ()}
def routes_match(app) -> bool:
    agent_rows = {route for route, owner in OWNERS.items() if owner == "agent-loop"}
    return len(agent_rows) == 6 and agent_rows <= route_set(app)


def store_app(home):
    os.makedirs(os.path.join(home, "db"), exist_ok=True)
    store = Store.open(os.path.join(home, "db", "w.db"), registry=build_registry())
    return store, create_app(store=store, start_listeners=False)
def test_create_app_mounts_the_six_routes_and_sets_the_agent_for_both_hub_modes(home):
    bare = create_app(Hub(), start_listeners=False)
    assert bare.state.agent is not None and routes_match(bare)
    store, app = store_app(home)
    try:
        assert app.state.agent is not None and routes_match(app)
        assert app.state.agent._ports.state is app.state.hub and app.state.agent._ports.registry is app.state.services
        assert app.state.agent._ports.approvals is app.state.approvals
    finally:
        store.close()
def test_code_route_uses_the_incident_services_code_folder_or_falls_back_to_the_code_root(home, tmp_path, monkeypatch):
    svc_dir = tmp_path / "svc"
    svc_dir.mkdir()
    (svc_dir / "app.py").write_text("raise OSError('ERROR quota exceeded for tenant acme')\n", encoding="utf-8")
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "legacy.py").write_text("raise OSError('ERROR quota exceeded for tenant acme')\n", encoding="utf-8")
    monkeypatch.setenv("CHIMERICMESH_CODE_ROOT", str(root_dir))
    async def go():
        hub = Hub()
        app = create_app(hub, start_listeners=False)
        await app.state.services.create_service(
            ServiceFields(id="svc", name="svc", hosts=("web01",), code_folder=str(svc_dir)), actor="t")
        await app.state.services.create_service(ServiceFields(id="bare", name="bare", hosts=("web02",)), actor="t")
        for host in ("web01", "web02", "web03"):
            for _ in range(3):
                await hub.ingest(Event(id=0, ts=hub.now(), source="syslog", host=host, severity=3,
                                       message="ERROR quota exceeded for tenant acme", app="app"))
        by_host = {i.host: i for i in hub.incidents()}
        async with client(app) as c:
            own = (await c.get(f"/api/incidents/{by_host['web01'].id}/code")).json()
            assert isinstance(own, list) and own and set(own[0]) == {"file", "line", "text"}
            assert own[0]["file"] == "app.py"
            for host in ("web02", "web03"):                     # no code folder, or no service: the code root
                fallback = (await c.get(f"/api/incidents/{by_host[host].id}/code")).json()
                assert fallback == hub.code_hints(by_host[host]) and fallback[0]["file"] == "legacy.py"
            assert (await c.get("/api/incidents/nope/code")).status_code == 404
    asyncio.run(go())
def test_lifespan_teardown_closes_the_queue_first(home):
    async def go():
        app = create_app(Hub(), start_listeners=False)
        async with app.router.lifespan_context(app):
            async with client(app) as c:
                held = asyncio.create_task(c.get("/api/watch/next", params={"timeout_seconds": 25}))
                await asyncio.sleep(0.3)
                assert app.state.agent.watch.waiting == 1
                start = time.monotonic()
                app.state.agent.aclose()                      # the same call the lifespan teardown makes first
                reply = await asyncio.wait_for(held, 2)
                assert reply.json()["status"] == "timeout" and time.monotonic() - start < 1.0
    asyncio.run(go())
def test_lifespan_exit_ends_a_held_waiter_without_waiting_for_it(home):
    async def go():
        app = create_app(Hub(), start_listeners=False)
        results = {}
        async def request():
            async with client(app) as c:
                results["reply"] = await c.get("/api/watch/next", params={"timeout_seconds": 25})
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        task = asyncio.create_task(request())
        await asyncio.sleep(0.3)
        assert app.state.agent.watch.waiting == 1
        start = time.monotonic()
        await cm.__aexit__(None, None, None)
        await asyncio.wait_for(task, 2)
        assert results["reply"].json()["status"] == "timeout"
        assert time.monotonic() - start < 2.5
    asyncio.run(go())
def test_a_real_uvicorn_server_stops_in_under_three_seconds_with_a_held_waiter(home):
    """R1-M2: uvicorn waits for in-flight requests before the lifespan teardown, so the exit path must wake them."""
    async def go():
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)   # uvicorn re-raises a captured signal at the end
        try:
            await serve_and_stop()
        finally:
            signal.signal(signal.SIGINT, previous)
    async def serve_and_stop():
        app = create_app(Hub(), start_listeners=False)
        server = entry.build_server(app, "127.0.0.1", 0)
        serving = asyncio.create_task(server.serve())
        for _ in range(300):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        outcome = {}
        async def held():
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
                    outcome["reply"] = await c.get("/api/watch/next", params={"timeout_seconds": 25})
            except httpx.HTTPError as exc:
                outcome["error"] = exc
        waiter = asyncio.create_task(held())
        for _ in range(200):
            if app.state.agent.watch.waiting == 1:
                break
            await asyncio.sleep(0.02)
        assert app.state.agent.watch.waiting == 1
        start = time.monotonic()
        server.handle_exit(signal.SIGINT, None)               # the server's signal path
        await asyncio.wait_for(serving, 6)
        took = time.monotonic() - start
        await asyncio.wait_for(waiter, 5)
        assert took < 3.0, took
        assert ("reply" in outcome and outcome["reply"].json()["status"] == "timeout") or "error" in outcome
    asyncio.run(go())
def test_build_server_config_has_the_graceful_timeout(home):
    app = create_app(Hub(), start_listeners=False)
    server = entry.build_server(app, "127.0.0.1", 8123)
    assert server.config.timeout_graceful_shutdown == 3
    assert server.config.host == "127.0.0.1" and server.config.port == 8123 and server.config.app is app
def test_the_exit_handler_wakes_the_queue_before_it_sets_should_exit(home):
    app = create_app(Hub(), start_listeners=False)
    server = entry.build_server(app, "127.0.0.1", 0)
    seen = []
    real = app.state.agent.aclose
    app.state.agent.aclose = lambda: (seen.append(server.should_exit), real())
    server.handle_exit(signal.SIGINT, None)
    assert seen == [False] and server.should_exit is True
def snippet_for(app, folder: str) -> str:
    async def go():
        await app.state.services.create_service(
            ServiceFields(id="svc", name="svc", hosts=("web01",), code_folder=folder), actor="t")
        async with client(app) as c:
            return (await c.get("/api/services/svc/permissions-snippet")).text
    return asyncio.run(go())
def test_workspace_root_from_the_environment_is_used(home, tmp_path, monkeypatch):
    (tmp_path / "ws" / "code").mkdir(parents=True)
    monkeypatch.setenv("CHIMERICMESH_WORKSPACE_ROOT", str(tmp_path / "ws"))
    text = snippet_for(create_app(Hub(), start_listeners=False), str(tmp_path / "ws" / "code"))
    assert 'match: ["code/**"]' in text and json.dumps(str(tmp_path / "ws")) in text
def test_an_invalid_workspace_root_logs_one_warning_and_emits_no_allow_line(home, tmp_path, monkeypatch, caplog):
    (tmp_path / "ws" / "code").mkdir(parents=True)
    monkeypatch.setenv("CHIMERICMESH_WORKSPACE_ROOT", str(tmp_path / "missing"))
    with caplog.at_level(logging.WARNING):
        app = create_app(Hub(), start_listeners=False)
    warnings = [r for r in caplog.records if "WORKSPACE_ROOT" in r.getMessage()]
    assert len(warnings) == 1
    text = snippet_for(app, str(tmp_path / "ws" / "code"))
    assert not [l for l in text.splitlines() if l.startswith("    match: [") and "/**" in l and "**/" not in l]
    assert "Kiro prompts for each write" in text
def test_cwd_src_walks_up_to_the_repo_root(home, tmp_path, monkeypatch):
    (tmp_path / ".kiro").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "examples" / "legacy_app").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "src")
    app = create_app(Hub(), start_listeners=False)
    assert os.path.normcase(app.state.agent._ports.workspace_root) == os.path.normcase(str(tmp_path))
    assert 'match: ["examples/legacy_app/**"]' in snippet_for(app, str(tmp_path / "examples" / "legacy_app"))
def golden_scenario() -> dict:
    """A fixed hub with a fixed clock: the three existing read routes, serialized."""
    async def go():
        hub = Hub(clock=lambda: 1_700_000_000.0)
        app = create_app(hub, start_listeners=False)
        for n in range(4):
            await hub.ingest(Event(id=0, ts=1_700_000_000.0 + n, source="syslog", host="db01", severity=3,
                                   message="FATAL: could not write block: No space left on device", app="postgres"))
        await hub.ingest(Event(id=0, ts=1_700_000_010.0, source="irc", host="web02", severity=5,
                               message="upstream timed out", app="irc"))
        iid = hub.incidents()[0].id
        await hub.create_proposal(iid, "free disk space", "remove old logs")
        async with client(app) as c:
            return {"events": (await c.get("/api/events", params={"limit": 20})).json(),
                    "incidents": (await c.get("/api/incidents")).json(),
                    "proposals": (await c.get("/api/proposals")).json()}
    return asyncio.run(go())
def test_existing_route_outputs_are_unchanged(home):
    assert golden_scenario() == json.loads(GOLDEN.read_text(encoding="utf-8"))
