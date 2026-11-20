"""Task 8.2 (criterion 1.1e): connectors behave the same on the plain hub and the store-backed hub."""
from __future__ import annotations

import asyncio
import socket

import pytest

from chimericmesh.api import build_registry, create_app
from chimericmesh.connectors.submit import make_submit
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.store import Store

NCSA_500 = '203.0.113.{n} - - [18/Nov/2026:10:00:0{n} +0000] "GET /orders/42 HTTP/1.1" 500 123\n'


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture(params=["plain", "store"])
def hub_app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_BIND", "127.0.0.1")
    for name in ("SYSLOG", "IRC", "TELNET"):
        monkeypatch.setenv(f"CHIMERICMESH_{name}_PORT", "0")
    port = free_port()
    monkeypatch.setenv("CHIMERICMESH_SYSLOG_TCP_PORT", str(port))
    if request.param == "plain":
        hub = Hub()
        app = create_app(hub, start_listeners=True, enforce_access=False)
        yield hub, app, port
        app.state.store.close()
        return
    store = Store.open(tmp_path / "h.db", registry=build_registry())
    hub = Hub(store=store)
    app = create_app(hub, store=store, start_listeners=True, enforce_access=False)
    yield hub, app, port
    store.close()


def scenario(hub, app, port, log) -> list[tuple]:
    async def main():
        async with app.router.lifespan_context(app):
            reg = app.state.services
            await reg.create_service(ServiceFields(id="web", name="web"), actor="t")
            await reg.create_service(ServiceFields(id="proxy", name="proxy", app_names=("proxy-worker",)), actor="t")
            await reg.add_log_source("web", LogSourceFields(path=str(log), format="ncsa"), actor="t")
            await asyncio.sleep(0.8)                   # the first poll attaches (poll interval 0.5 s)
            with open(log, "a") as f:
                f.write("".join(NCSA_500.format(n=n) for n in (1, 2, 3)))
            await until(lambda: len([e for e in hub.recent(50) if e.source == "file"]) == 3)
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"<27>Nov 18 10:00:01 gw01 proxy-worker[7]: upstream reset\n")
            await w.drain()
            await until(lambda: any(e.app == "proxy-worker" for e in hub.recent(50)))
            w.close()
            submitted = await make_submit(hub.ingest)(
                Event("file", "m/web", "web", 6, "direct submit"), "ls1")
            assert submitted.service_id == "web" and submitted.id and submitted.id > 0
            incidents = hub.incidents(status="open")
            file_inc = [i for i in incidents if i.service_id == "web"]
            assert len(file_inc) == 1
            assert file_inc[0].host.endswith("/web")
            return sorted((e.service_id, e.source, e.severity, e.message) for e in hub.recent(50))

    return asyncio.run(main())


def test_file_and_tcp_events_are_tagged_on_both_hubs(hub_app, tmp_path):
    hub, app, port = hub_app
    log = tmp_path / "access.log"
    log.write_text("")
    results = scenario(hub, app, port, log)
    files = [r for r in results if r[1] == "file" and r[3] != "direct submit"]
    assert files == [("web", "file", 3, "GET /orders/42 -> 500")] * 3
    assert ("proxy", "syslog", 3, "upstream reset") in results
    # both parametrizations must give this exact list; the reference is computed from the plain run below
    _RESULTS.setdefault("runs", []).append(results)
    if len(_RESULTS["runs"]) == 2:
        assert _RESULTS["runs"][0] == _RESULTS["runs"][1]


_RESULTS: dict = {}


def test_store_backed_restart_resumes_at_the_stored_offset(tmp_path, conn_registry, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_SYSLOG_TCP_PORT", "0")
    log = tmp_path / "access.log"
    log.write_text("")
    store = conn_registry

    def messages(hub) -> list[str]:
        return [e.message for e in hub.recent(100) if e.source == "file"]

    async def first():
        hub = Hub(store=store)
        app = create_app(hub, store=store, start_listeners=False, enforce_access=False)
        async with app.router.lifespan_context(app):
            reg = app.state.services
            await reg.create_service(ServiceFields(id="web", name="web"), actor="t")
            await reg.add_log_source("web", LogSourceFields(path=str(log), format="plain"), actor="t")
            await asyncio.sleep(0.8)
            with open(log, "a") as f:
                f.write("a1\na2\n")
            await until(lambda: len(messages(hub)) == 2)

    async def second():
        hub = Hub(store=store)
        app = create_app(hub, store=store, start_listeners=False, enforce_access=False)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(1.2)
            got = [e.message for e in await hub.events_after(0, 100) if e.source == "file"]
            return got

    asyncio.run(first())
    with open(log, "a") as f:
        f.write("b1\n")
    got = asyncio.run(second())
    assert got == ["a1", "a2", "b1"]                    # each line once, nothing repeated after the restart
