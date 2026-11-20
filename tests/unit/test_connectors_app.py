"""Task 8.1: ``create_app`` wiring of the connectors: migrations, state objects, lifespan, shutdown order."""
from __future__ import annotations

import asyncio
import gc
import logging
import socket
import threading
import time

import httpx
import pytest

from chimericmesh import api as api_mod
from chimericmesh.api import build_registry, create_app
from chimericmesh.connectors.manager import ConnectorManager
from chimericmesh.hub import Hub
from chimericmesh.parsers.formats import FORMAT_NAMES
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store

SYSLOG_LINE = b"<27>Nov 13 11:10:08 db01 pg[1]: FATAL: no space left\n"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def alt_ports(monkeypatch, tcp: int | str | None = None) -> None:
    """Never touch the default ports: the three old listeners bind ephemeral ports."""
    monkeypatch.setenv("CHIMERICMESH_BIND", "127.0.0.1")
    for name in ("SYSLOG", "IRC", "TELNET"):
        monkeypatch.setenv(f"CHIMERICMESH_{name}_PORT", "0")
    if tcp is not None:
        monkeypatch.setenv("CHIMERICMESH_SYSLOG_TCP_PORT", str(tcp))


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def has_table(store, name: str) -> bool:
    return store.read_sync(lambda tx: tx.one("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)) is not None,
                           allow_blocking=True)


# ---- migrations and state objects -------------------------------------------------------------------


def test_build_registry_includes_the_connectors_migration(tmp_path):
    store = Store.open(tmp_path / "a.db", registry=build_registry())
    try:
        assert has_table(store, "tail_offsets")
    finally:
        store.close()


def test_a_file_store_opened_by_create_app_has_tail_offsets(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    app = create_app(start_listeners=False)
    try:
        assert has_table(app.state.store, "tail_offsets")
    finally:
        app.state.store.close()


def test_state_objects_exist_without_listeners():
    app = create_app(Hub(), start_listeners=False)
    try:
        assert isinstance(app.state.connectors, ConnectorManager)
        assert app.state.syslog_tcp is None
        assert app.state.services.known_formats == frozenset(FORMAT_NAMES)
    finally:
        app.state.store.close()


def test_building_the_app_without_a_lifespan_leaves_no_thread_or_task_behind():
    gc.collect()
    before = threading.active_count()
    app = create_app(Hub(), start_listeners=False)
    app.state.store.close()
    del app
    gc.collect()
    assert threading.active_count() == before


def test_a_dropped_app_does_not_keep_its_private_store_alive_through_the_hub():
    """The hub outlives the app (``hub.tagger`` -> registry -> subscription -> manager); the manager must
    not keep the private ``:memory:`` store, and so its writer thread, alive (same rule as services)."""
    import weakref
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    ref = weakref.ref(app.state.store)
    del app
    gc.collect()
    assert ref() is None


# ---- lifespan with a real file -----------------------------------------------------------------------


async def register_file(app, path, fmt="plain", sid="web") -> None:
    reg = app.state.services
    await reg.create_service(ServiceFields(id=sid, name=sid), actor="t")
    await reg.add_log_source(sid, LogSourceFields(path=str(path), format=fmt), actor="t")


def test_lifespan_tails_a_registered_file_and_shows_status(tmp_path, monkeypatch):
    alt_ports(monkeypatch, tcp=0)
    store = Store.open(tmp_path / "m.db", registry=build_registry())
    hub = Hub(store=store)
    app = create_app(hub, store=store, start_listeners=True, enforce_access=False)
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        async with app.router.lifespan_context(app):
            assert app.state.syslog_tcp is None            # CHIMERICMESH_SYSLOG_TCP_PORT=0
            await register_file(app, log)
            await asyncio.sleep(0.2)
            with open(log, "a") as f:
                f.write("hello from the file\n")
            await until(lambda: any(e.message == "hello from the file" for e in hub.recent(10)))
            ev = [e for e in hub.recent(10) if e.message == "hello from the file"][0]
            assert ev.source == "file" and ev.service_id == "web"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                body = (await c.get("/api/services/web")).json()
            src = body["log_sources"][0]
            assert src["status"] == "running" and src["events_emitted"] == 1

    asyncio.run(main())
    store.close()


def test_store_without_connectors_migration_still_works_with_one_warning(tmp_path, caplog):
    store = Store.open(tmp_path / "s.db", registry=services_registry())
    hub = Hub(store=store)
    app = create_app(hub, store=store, start_listeners=False, enforce_access=False)
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            async with app.router.lifespan_context(app):
                await register_file(app, log)
                await asyncio.sleep(0.2)
                with open(log, "a") as f:
                    f.write("no offsets\n")
                await until(lambda: any(e.message == "no offsets" for e in hub.recent(10)))
                await asyncio.sleep(0.3)

    asyncio.run(main())
    store.close()
    assert len([r for r in caplog.records if "tail_offsets" in r.getMessage()]) == 1


def test_tcp_listener_binds_the_configured_address_and_delivers(tmp_path, monkeypatch):
    port = free_port()
    alt_ports(monkeypatch, tcp=port)
    hub = Hub()
    app = create_app(hub, start_listeners=True, enforce_access=False)

    async def main():
        async with app.router.lifespan_context(app):
            server = app.state.syslog_tcp
            assert server is not None and server.port == port
            assert server._server.sockets[0].getsockname()[0] == "127.0.0.1"
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(SYSLOG_LINE)
            await w.drain()
            await until(lambda: any("no space left" in e.message for e in hub.recent(10)))
            w.close()

    asyncio.run(main())
    app.state.store.close()


def test_tcp_port_in_use_starts_the_app_with_one_warning(monkeypatch, caplog):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    alt_ports(monkeypatch, tcp=blocker.getsockname()[1])
    app = create_app(Hub(), start_listeners=True, enforce_access=False)

    async def main():
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            async with app.router.lifespan_context(app):
                assert app.state.syslog_tcp is None

    try:
        asyncio.run(main())
    finally:
        blocker.close()
        app.state.store.close()
    assert len([r for r in caplog.records if "syslog TCP port" in r.getMessage()]) == 1


# ---- shutdown order and budget ------------------------------------------------------------------------


class FakeCloser:
    def __init__(self, name, calls) -> None:
        self.name, self.calls = name, calls

    def close(self) -> None:
        self.calls.append((self.name, None))


def test_shutdown_order_and_remaining_time(monkeypatch):
    alt_ports(monkeypatch, tcp=0)
    calls: list[tuple[str, float | None]] = []

    async def fake_syslog(hub, host, port):
        return FakeCloser("udp", calls)

    async def fake_telnet(hub, host, port):
        return FakeCloser("telnet", calls)

    class FakeIrc:
        def __init__(self, hub) -> None:
            pass

        async def start(self, host, port):
            return FakeCloser("irc", calls)

    class FakeTcp:
        async def aclose(self, timeout=1.0):
            calls.append(("tcp", timeout))

    async def fake_tcp(submit, host, port, cfg=None):
        return FakeTcp()

    monkeypatch.setattr(api_mod, "start_syslog", fake_syslog)
    monkeypatch.setattr(api_mod, "start_telnet", fake_telnet)
    monkeypatch.setattr(api_mod, "IrcServer", FakeIrc)
    monkeypatch.setattr(api_mod, "start_syslog_tcp", fake_tcp)
    monkeypatch.setenv("CHIMERICMESH_SYSLOG_TCP_PORT", "5999")      # never bound: the start is faked
    hub = Hub()
    app = create_app(hub, start_listeners=True, enforce_access=False)
    t0 = time.monotonic()
    orig_manager, orig_limiter, orig_hub = app.state.connectors.aclose, app.state.reduce_limiter.aclose, hub.aclose
    store = app.state.store
    orig_store_close = store.close

    async def manager_close(timeout=1.0):
        calls.append(("manager", timeout))
        await orig_manager(timeout)

    async def limiter_close():
        calls.append(("limiter", time.monotonic() - t0))
        await orig_limiter()

    async def hub_close(deadline=5.0):
        calls.append(("hub", deadline))
        await orig_hub(deadline)

    def store_close(*a, **k):
        calls.append(("store", None))
        return orig_store_close(*a, **k)

    monkeypatch.setattr(app.state.connectors, "aclose", manager_close)
    monkeypatch.setattr(app.state.reduce_limiter, "aclose", limiter_close)
    monkeypatch.setattr(hub, "aclose", hub_close)
    monkeypatch.setattr(store, "close", store_close)

    async def main():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.syslog_tcp, FakeTcp)

    asyncio.run(main())
    names = [n for n, _ in calls]
    assert set(names[:3]) == {"udp", "telnet", "irc"}
    assert names[3:] == ["tcp", "manager", "limiter", "hub", "store"]
    by = dict(calls)
    assert by["tcp"] <= 0.4 and by["manager"] <= 0.6 and by["hub"] >= 3.0


def test_shutdown_stays_inside_five_seconds_with_a_slow_client_busy_tailers_and_a_blocked_store(tmp_path, monkeypatch):
    port = free_port()
    alt_ports(monkeypatch, tcp=port)
    store = Store.open(tmp_path / "m.db", registry=build_registry())
    hub = Hub(store=store)
    app = create_app(hub, store=store, start_listeners=True, enforce_access=False)
    seen = {}
    orig_hub = hub.aclose

    async def hub_close(deadline=5.0):
        seen["hub"] = deadline
        await asyncio.sleep(0)

    monkeypatch.setattr(hub, "aclose", hub_close)
    paths = [tmp_path / f"{i}.log" for i in range(20)]
    for p in paths:
        p.write_text("")

    async def main():
        async with app.router.lifespan_context(app):
            reg = app.state.services
            await reg.create_service(ServiceFields(id="web", name="web"), actor="t")
            for p in paths:
                await reg.add_log_source("web", LogSourceFields(path=str(p), format="plain"), actor="t")
            await asyncio.sleep(0.3)
            r, w = await asyncio.open_connection("127.0.0.1", port)     # never reads
            w.write(SYSLOG_LINE)
            for p in paths:
                with open(p, "a") as f:
                    f.write("".join(f"line {i}\n" for i in range(300)))
            await asyncio.sleep(0.1)

            async def never(*a, **k):
                await asyncio.Event().wait()

            store.transact = never
        w.close()

    t0 = time.monotonic()
    asyncio.run(main())
    elapsed = time.monotonic() - t0
    del store.transact
    asyncio.run(orig_hub(1.0))
    store.close()
    assert elapsed < 5.0
    assert seen["hub"] >= 3.0
