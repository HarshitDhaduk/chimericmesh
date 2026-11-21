"""``create_app`` wiring of discovery: migration, router, lifespan step, no scan unless asked (R1.1, R3.1, R3.5)."""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from chimericmesh import api as api_mod
from chimericmesh.api import build_registry, create_app
from chimericmesh.discovery.catalog import CatalogLoad
from chimericmesh.discovery.models import RunResult
from chimericmesh.hub import Hub
from chimericmesh.parsers.formats import FORMAT_NAMES
from chimericmesh.services.validate import DEFAULT_FORMATS
from chimericmesh.store import Store
from chimericmesh.store.config import MEMORY


class SpyRunner:
    calls: list = []

    def __init__(self, os_name, environ):
        self.os_name = os_name

    def run(self, tool, *, timeout, max_bytes, cancel):
        SpyRunner.calls.append(tool.name)
        return RunResult(False, "", "missing")


@pytest.fixture(autouse=True)
def spy(monkeypatch):
    SpyRunner.calls = []
    monkeypatch.setattr(api_mod, "SubprocessRunner", SpyRunner)
    return SpyRunner


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(run())


def test_build_registry_includes_the_discovery_migration():
    store = Store.open(MEMORY, registry=build_registry())
    try:
        names = store.read_sync(lambda tx: [r["name"] for r in tx.all("SELECT name FROM migrations")], allow_blocking=True)
        version = store.read_sync(lambda tx: tx.one("PRAGMA user_version")[0], allow_blocking=True)
        assert "discovery:0300-dismissals" in names and "connectors:0200-tail-offsets" in names and version >= 300
    finally:
        store.close()


def test_app_state_has_discovery_with_a_bare_hub_and_with_a_store_backed_hub(tmp_path):
    bare = create_app(Hub(), start_listeners=False)
    assert bare.state.discovery is not None and bare.state.discovery.load.catalog is not None
    store = Store.open(tmp_path / "w.db", registry=build_registry())
    try:
        backed = create_app(Hub(store=store), start_listeners=False)
        assert backed.state.discovery.manager is not None
        r = call(backed, "POST", "/api/discovery/scan")
        assert r.status_code == 202                       # injected hub: enforcement off unless asked
    finally:
        store.close()


def test_with_enforcement_on_the_scan_route_is_401_and_nothing_runs_until_the_token_is_sent(tmp_path, monkeypatch, spy):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    app = create_app(start_listeners=False)
    try:
        assert app.state.access.enforce is True
        assert call(app, "POST", "/api/discovery/scan").status_code == 401
        assert call(app, "GET", "/api/discovery/scans/sc1").status_code == 401
        assert call(app, "GET", "/api/discovery/dismissals").status_code == 401
        assert spy.calls == []
        token = app.state.access.tokens.current()
        assert call(app, "POST", "/api/discovery/scan", headers={"X-Admin-Token": token}).status_code == 202
    finally:
        app.state.store.close()


def test_enforcement_off_gives_202_and_a_failed_scan_when_every_tool_is_missing(spy):
    app = create_app(Hub(), start_listeners=False, enforce_access=False)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.post("/api/discovery/scan")
            assert r.status_code == 202
            for _ in range(200):
                body = (await c.get(f"/api/discovery/scans/{r.json()['scan_id']}")).json()
                if body["state"] != "running":
                    return body
                await asyncio.sleep(0.01)
        raise AssertionError("scan did not finish")
    body = asyncio.run(go())
    assert body["state"] == "failed" and body["reason"].startswith("no scan tool could run")
    assert spy.calls                                     # the (spy) runner was asked, by the explicit POST only


def test_building_the_app_and_using_every_other_route_never_starts_a_scan(spy):
    app = create_app(Hub(), start_listeners=False, enforce_access=False)

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                for url in ("/api/stats", "/api/events", "/api/incidents", "/api/proposals", "/api/services", "/"):
                    assert (await c.get(url)).status_code == 200
                await asyncio.sleep(0.2)
    asyncio.run(go())
    assert spy.calls == [] and app.state.discovery.manager.get("sc1") is None


def test_the_shipped_catalog_loads_inside_create_app_with_the_registry_formats():
    assert DEFAULT_FORMATS == frozenset(FORMAT_NAMES)
    app = create_app(Hub(), start_listeners=False)
    catalog = app.state.discovery.load.catalog
    assert catalog is not None and len(catalog.entries) >= 14
    known = app.state.services.known_formats
    assert all(rule.format in known for e in catalog.entries for rules in e.log_paths.values() for rule in rules)


def test_the_platform_name_is_computed_once_and_none_still_starts_the_app(monkeypatch):
    seen = []

    def fake(platform):
        seen.append(platform)
        return None
    monkeypatch.setattr(api_mod, "os_name_from_platform", fake)
    app = create_app(Hub(), start_listeners=False, enforce_access=False)
    assert len(seen) == 1

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.post("/api/discovery/scan")
            for _ in range(100):
                body = (await c.get(f"/api/discovery/scans/{r.json()['scan_id']}")).json()
                if body["state"] != "running":
                    return body
                await asyncio.sleep(0.01)
    body = asyncio.run(go())
    assert body["state"] == "failed" and body["reason"] == "unsupported platform"


def test_a_catalog_that_cannot_load_leaves_the_app_running_and_scan_is_503(monkeypatch):
    monkeypatch.setattr(api_mod, "load_catalog", lambda formats: CatalogLoad(None, "broken"))
    app = create_app(Hub(), start_listeners=False, enforce_access=False)
    assert call(app, "GET", "/api/stats").status_code == 200
    assert call(app, "GET", "/api/services").status_code == 200
    r = call(app, "POST", "/api/discovery/scan")
    assert r.status_code == 503 and "catalog" in r.json()["detail"]


def test_lifespan_exit_does_not_wait_for_a_blocked_scan_and_stops_it(monkeypatch):
    started = threading.Event()
    seen = {}

    def blocked(inputs, runner, procfs, fs, next_id, cancel):
        seen["cancel"] = cancel
        started.set()
        end = time.monotonic() + 10                        # safety timeout: never a bare wait
        while not cancel.is_set() and time.monotonic() < end:
            time.sleep(0.01)
        raise RuntimeError("stopped")
    monkeypatch.setattr(api_mod, "run_scan_sync", blocked)
    app = create_app(Hub(), start_listeners=False, enforce_access=False)

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                assert (await c.post("/api/discovery/scan")).status_code == 202
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
        return time.monotonic()
    t0 = time.monotonic()
    t1 = asyncio.run(go())
    assert t1 - t0 < 4.0
    assert seen["cancel"].is_set()
    assert app.state.discovery.manager._executor._thread_name_prefix == "discovery-scan"   # its own executor
