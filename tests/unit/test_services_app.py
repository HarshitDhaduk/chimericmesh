"""``create_app`` wiring of the services module: flags, registry, access, limiter, shutdown (R6.1, R7.4 to R7.6, R10.3)."""
from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time

import httpx
import pytest

from chimericmesh import api as api_mod
from chimericmesh.api import build_registry, create_app
from chimericmesh.hub import Hub
from chimericmesh.services import ServicesNotMigrated
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store
from chimericmesh.store.schema import core_registry


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(run())


# ---- effective enforce_access (R7.5) ------------------------------------------------------------

def test_injected_hub_and_none_means_off():
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    assert hub.enforce_access is False
    assert app.state.access.enforce is False and hub.access is None


def test_no_hub_means_on_with_a_temporary_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    app = create_app(start_listeners=False)
    try:
        assert app.state.hub.enforce_access is True
        assert app.state.access.enforce is True and app.state.hub.access is app.state.access
    finally:
        app.state.store.close()


@pytest.mark.parametrize("value", [True, False])
def test_explicit_value_wins(value):
    hub = Hub()
    create_app(hub, start_listeners=False, enforce_access=value)
    assert hub.enforce_access is value


def test_hub_built_with_enforcement_but_passed_none_ends_off_and_warns_once(caplog):
    hub = Hub(enforce_access=True)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.api"):
        create_app(hub, start_listeners=False)
    assert hub.enforce_access is False
    warnings = [r for r in caplog.records if "enforce_access" in r.getMessage()]
    assert len(warnings) == 1


# ---- state, registry, tagger ----------------------------------------------------------------------

def test_state_objects_exist_and_tagger_is_the_registry_tag():
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    assert app.state.services is not None and app.state.reduce_limiter is not None
    assert hub.tagger == app.state.services.tag


def test_bare_hub_registry_lives_in_the_private_memory_store():
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    assert app.state.store.path == ":memory:"
    r = call(app, "GET", "/api/services")
    assert r.status_code == 200 and [s["id"] for s in r.json()] == ["unassigned"]
    r = call(app, "POST", "/api/services", json={"id": "web", "name": "Web"})
    assert r.status_code == 201
    assert [s["id"] for s in call(app, "GET", "/api/services").json()] == ["web", "unassigned"]
    app.state.store.close()


def test_store_without_services_migrations_raises_and_with_them_works(tmp_path):
    bad = Store.open(tmp_path / "a.db", registry=core_registry())
    try:
        with pytest.raises(ServicesNotMigrated):
            create_app(Hub(), store=bad, start_listeners=False)
        with pytest.raises(ServicesNotMigrated):
            create_app(store=bad, start_listeners=False)
    finally:
        bad.close()
    good = Store.open(tmp_path / "b.db", registry=services_registry())
    try:
        app = create_app(Hub(), store=good, start_listeners=False)
        assert app.state.store is good
    finally:
        good.close()


def test_build_registry_has_core_and_services_tables():
    s = Store.open(":memory:", registry=build_registry())
    try:
        tables = {r[0] for r in s.read_sync(lambda tx: tx.all("SELECT name FROM sqlite_master WHERE type='table'"),
                                            allow_blocking=True)}
        assert {"events", "services", "log_sources", "service_tombstones"} <= tables
    finally:
        s.close()


# ---- token ----------------------------------------------------------------------------------------

def test_memory_database_keeps_the_token_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", ":memory:")
    app = create_app(start_listeners=False)
    try:
        assert app.state.access.token_path is None
        assert app.state.access.tokens.current()
        assert not (tmp_path / "cm-home" / "admin.token").exists()
    finally:
        app.state.store.close()


def test_file_database_writes_the_token_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "x" / "m.db"))
    app = create_app(start_listeners=False)
    try:
        assert app.state.access.token_path == tmp_path / "cm-home" / "admin.token"
        assert app.state.access.token_path.exists()
    finally:
        app.state.store.close()


# ---- bind warning ---------------------------------------------------------------------------------

def test_non_loopback_bind_warns_once_and_loopback_does_not(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="chimericmesh.api"):
        monkeypatch.setenv("CHIMERICMESH_BIND", "127.0.0.1")
        create_app(Hub(), start_listeners=False)
        assert not [r for r in caplog.records if "CHIMERICMESH_BIND" in r.getMessage()]
        monkeypatch.setenv("CHIMERICMESH_BIND", "0.0.0.0")
        create_app(Hub(), start_listeners=False)
    assert len([r for r in caplog.records if "CHIMERICMESH_BIND" in r.getMessage()]) == 1


# ---- shutdown order and deadline ------------------------------------------------------------------

def run_lifespan(app):
    async def go():
        async with app.router.lifespan_context(app):
            pass
    start = time.monotonic()
    asyncio.run(go())
    return time.monotonic() - start


def test_shutdown_order_and_remaining_time(monkeypatch):
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    calls: list[tuple[str, float]] = []
    t0 = time.monotonic()
    limiter = app.state.reduce_limiter
    orig_limiter, orig_hub = limiter.aclose, hub.aclose

    async def limiter_close():
        calls.append(("limiter", time.monotonic() - t0))
        await orig_limiter()

    async def hub_close(deadline=5.0):
        calls.append(("hub", deadline))
        await orig_hub(deadline)

    monkeypatch.setattr(limiter, "aclose", limiter_close)
    monkeypatch.setattr(hub, "aclose", hub_close)
    run_lifespan(app)
    assert [n for n, _ in calls] == ["limiter", "hub"]
    assert calls[1][1] >= 4.0


def test_shutdown_stays_inside_five_seconds_with_a_blocked_limiter(tmp_path, monkeypatch):
    store = Store.open(tmp_path / "s.db", registry=build_registry())
    hub = Hub(store=store)
    app = create_app(hub, store=store, start_listeners=False)
    seen = {}

    async def stuck():
        await asyncio.sleep(30)

    async def hub_close(deadline=5.0):
        seen["hub"] = deadline

    monkeypatch.setattr(app.state.reduce_limiter, "aclose", stuck)
    monkeypatch.setattr(hub, "aclose", hub_close)
    elapsed = run_lifespan(app)
    assert elapsed < 5.0
    assert seen["hub"] >= 4.0
    store.close()


def test_building_the_app_leaves_no_thread_or_task_behind():
    gc.collect()
    before = threading.active_count()
    app = create_app(Hub(), start_listeners=False)
    app.state.store.close()
    del app
    gc.collect()
    assert threading.active_count() == before


# ---- registry and status through the app on both hub kinds (M2) -----------------------------------

@pytest.fixture(params=["plain", "store"])
def app_pair(request, tmp_path):
    if request.param == "plain":
        app = create_app(Hub(), start_listeners=False, enforce_access=False)
        yield app
        app.state.store.close()
        return
    store = Store.open(tmp_path / "m.db", registry=build_registry())
    hub = Hub(store=store)
    yield create_app(hub, store=store, start_listeners=False, enforce_access=False)
    asyncio.run(hub.aclose())
    store.close()


def test_registry_status_and_tagging_through_the_app(app_pair):
    app = app_pair
    assert call(app, "POST", "/api/services", json={"id": "web", "name": "W", "hosts": ["db01"]}).status_code == 201
    body = {"source": "syslog", "line": "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: no space left on device 1",
            "host": "db01"}
    for _ in range(3):
        assert call(app, "POST", "/api/ingest", json=body).status_code == 202
    web = call(app, "GET", "/api/services/web").json()
    assert web["events_total"] == 3 and web["state"] == "active" and web["open_incidents"] == 1
    assert call(app, "GET", "/api/events?service_id=web").json()[0]["service_id"] == "web"
