"""create_app store wiring: one test per row of the design's create_app contract, plus REST changes."""
import asyncio
import gc
import inspect
import os
import threading
import time

import httpx
import pytest

import chimericmesh.api as api
from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.errors import StoreCorruptError, StoreWriteError
from chimericmesh.api import build_registry
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"
BASELINE = {"id", "incident_id", "action", "details", "status", "created", "decided_by", "decided_at",
            "result_note"}


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, url, **kw)

    return asyncio.run(run())


async def lifespan(app, body=None):
    """Run the app's lifespan around ``body`` (startup, then shutdown)."""
    async with app.router.lifespan_context(app):
        if body is not None:
            await body()


def test_signature():
    params = inspect.signature(create_app).parameters
    assert list(params)[:2] == ["hub", "start_listeners"]
    assert params["store"].kind is inspect.Parameter.KEYWORD_ONLY and params["store"].default is None


def test_no_hub_opens_the_configured_database(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("CHIMERICMESH_DB", str(path))
    app = create_app(start_listeners=False)
    hub, store = app.state.hub, app.state.store
    assert hub.store is store and store.path == str(path) and path.exists()
    assert hub.audit is store.audit
    asyncio.run(lifespan(app))
    assert store._closed


def test_no_hub_uses_the_home_default(tmp_path):
    app = create_app(start_listeners=False)
    assert app.state.store.path == os.path.join(os.environ["CHIMERICMESH_HOME"], "chimericmesh.db")
    asyncio.run(lifespan(app))


def test_bare_hub_gets_a_private_memory_store_and_no_thread_leaks(tmp_path):
    before_files = set(os.listdir(os.environ.get("CHIMERICMESH_HOME", tmp_path))) if os.path.isdir(
        os.environ["CHIMERICMESH_HOME"]) else set()
    start = threading.active_count()
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    store = app.state.store
    assert store.path == ":memory:" and hub.store is None and hub.audit is store.audit
    assert hub.lifecycle._audit_getter() is store.audit
    assert call(app, "GET", "/api/stats").status_code == 200
    assert not os.path.isdir(os.environ["CHIMERICMESH_HOME"]) or set(
        os.listdir(os.environ["CHIMERICMESH_HOME"])) == before_files
    del app, store
    gc.collect()
    assert threading.active_count() <= start


def test_bare_hub_keeps_an_existing_audit_sink():
    sink = object()
    hub = Hub(audit=sink)  # type: ignore[arg-type]
    create_app(hub, start_listeners=False)
    assert hub.audit is sink


def test_store_backed_hub_uses_its_store(tmp_path):
    s = Store.open(tmp_path / "s.db", registry=build_registry())
    hub = Hub(store=s)
    audit = hub.audit
    app = create_app(hub, start_listeners=False)
    assert app.state.store is s and hub.audit is audit
    asyncio.run(lifespan(app))
    assert not s._closed  # the caller that built the hub closes it
    s.close()


def test_store_only_builds_a_hub_on_it(tmp_path):
    s = Store.open(tmp_path / "s.db", registry=build_registry())
    app = create_app(store=s, start_listeners=False)
    assert app.state.hub.store is s and app.state.store is s and app.state.hub.audit is s.audit
    asyncio.run(lifespan(app))
    assert not s._closed
    s.close()


def test_bare_hub_with_a_store(tmp_path):
    s = Store.open(":memory:", registry=build_registry())
    hub = Hub()
    app = create_app(hub, store=s, start_listeners=False)
    assert app.state.store is s and hub.store is None and hub.audit is s.audit
    hub2 = Hub(audit="kept")  # type: ignore[arg-type]
    create_app(hub2, store=s, start_listeners=False)
    assert hub2.audit == "kept"
    s.close()


def test_store_backed_hub_with_another_store_raises(tmp_path):
    s = Store.open(tmp_path / "a.db", registry=core_registry())
    other = Store.open(":memory:", registry=core_registry())
    with pytest.raises(ValueError):
        create_app(Hub(store=s), store=other, start_listeners=False)
    s.close()
    other.close()


def test_memory_env_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", ":memory:")
    monkeypatch.chdir(tmp_path)
    app = create_app(start_listeners=False)
    asyncio.run(lifespan(app))
    assert os.listdir(tmp_path) == [] and not os.path.exists(os.environ["CHIMERICMESH_HOME"])


def test_shutdown_order_and_failing_store_finish_within_5_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "f.db"))
    app = create_app(start_listeners=False)
    hub, store = app.state.hub, app.state.store
    order = []
    real_aclose, real_close = hub.aclose, store.close

    async def aclose(deadline=5.0):
        order.append("hub.aclose")
        await real_aclose(deadline)

    def close(timeout=5.0, *, clean=True):
        order.append("store.close")
        real_close(timeout, clean=clean)

    hub.aclose, store.close = aclose, close

    async def fail(fn):
        raise StoreWriteError("disk gone")

    async def body():
        for _ in range(5):
            await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0))
        store.transact = fail

    t0 = time.monotonic()
    asyncio.run(lifespan(app, body))
    assert time.monotonic() - t0 < 5.5
    assert order == ["hub.aclose", "store.close"]


def test_listeners_close_before_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", ":memory:")
    order = []

    class Closer:
        def __init__(self, name):
            self.name = name

        def close(self):
            order.append(self.name)

    async def fake_syslog(hub, bind, port):
        return Closer("syslog")

    async def fake_telnet(hub, bind, port):
        return Closer("telnet")

    async def fake_irc_start(self, bind, port):
        return Closer("irc")

    monkeypatch.setattr(api, "start_syslog", fake_syslog)
    monkeypatch.setattr(api, "start_telnet", fake_telnet)
    monkeypatch.setattr(api.IrcServer, "start", fake_irc_start)
    app = create_app()
    hub = app.state.hub
    real = hub.aclose

    async def aclose(deadline=5.0):
        order.append("hub")
        await real(deadline)

    hub.aclose = aclose
    asyncio.run(lifespan(app))
    assert order == ["syslog", "telnet", "irc", "hub"]


def test_startup_failure_closes_the_store_unclean(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    monkeypatch.setenv("CHIMERICMESH_DB", str(path))

    async def boom(hub, bind, port):
        raise OSError("port in use")

    monkeypatch.setattr(api, "start_syslog", boom)
    app = create_app()
    with pytest.raises(OSError):
        asyncio.run(lifespan(app))
    s = Store.open(path, registry=build_registry())
    assert s.previous_shutdown_clean is False  # the failed start left the marker at 0
    s.close()
    s = Store.open(path, registry=build_registry())
    assert s.previous_shutdown_clean is True  # a normal close sets it to 1
    s.close()


def seeded_app():
    hub = Hub()

    async def seed():
        for _ in range(3):
            await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0, service_id="svc1"))

    asyncio.run(seed())
    return hub, create_app(hub, start_listeners=False)


def test_rest_validation():
    hub, app = seeded_app()
    for url in ("/api/proposals?limit=0", "/api/proposals?limit=501", "/api/proposals?service_id=" + "x" * 65,
                "/api/incidents?limit=0", "/api/incidents?limit=501", "/api/incidents?status=bad",
                "/api/incidents?service_id=" + "x" * 65, "/api/events?after_id=-1", "/api/events?after_id=a"):
        assert call(app, "GET", url).status_code == 422, url
    assert len(call(app, "GET", "/api/events?limit=9999").json()) <= 500
    assert call(app, "GET", "/api/events?after_id=0&limit=0").status_code == 200
    inc = call(app, "GET", "/api/incidents").json()[0]
    assert inc["service_id"] == "svc1" and inc["opening"] == 1
    call(app, "POST", "/api/proposals", json={"incident_id": inc["id"], "action": "fix"})
    assert set(call(app, "GET", "/api/proposals?service_id=svc1").json()[0]) == BASELINE
    stats = call(app, "GET", "/api/stats").json()
    assert stats["store_ok"] is True and stats["dropped_events"] == 0


def test_store_error_in_a_route_is_503(tmp_path):
    s = Store.open(tmp_path / "e.db", registry=build_registry())
    hub = Hub(store=s)

    async def seed():
        for _ in range(3):
            await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0))

    asyncio.run(seed())
    app = create_app(hub, start_listeners=False)

    async def fail(fn):
        raise StoreWriteError("disk gone: /secret/path")

    s.transact = fail
    r = call(app, "POST", "/api/proposals", json={"incident_id": hub.incidents()[0].id, "action": "fix"})
    assert r.status_code == 503 and r.json() == {"detail": "store unavailable"}
    r = call(app, "POST", "/api/terminal", json={"text": "!approve p1"})
    assert r.status_code == 200
    s.close()


def test_corrupt_database_raises(tmp_path, monkeypatch):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not a database" * 20)
    monkeypatch.setenv("CHIMERICMESH_DB", str(path))
    with pytest.raises(StoreCorruptError):
        create_app(start_listeners=False)


def test_main_exits_2_on_a_store_open_error(tmp_path, monkeypatch, capsys):
    import chimericmesh.__main__ as main

    path = tmp_path / "bad.db"
    path.write_bytes(b"not a database" * 20)
    monkeypatch.setenv("CHIMERICMESH_DB", str(path))
    with pytest.raises(SystemExit) as info:
        main.main()
    assert info.value.code == 2
    assert "is not a valid SQLite database; it was left untouched" in capsys.readouterr().err
