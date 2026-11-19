"""Status snapshot, unassigned pseudo service and the provider (R5.1, R5.4 to R5.7).
Parametrized over a plain ``Hub()`` and a store-backed hub (review M2).
"""
from __future__ import annotations
import asyncio
import logging
import os
import time

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import LogSourceFields, ServiceFields, SourceRuntime
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store

SKIP_BENCH = os.environ.get("CHIMERICMESH_SKIP_BENCH") == "1"


@pytest.fixture(params=["plain", "store"])
def env(request, tmp_path):
    if request.param == "plain":
        store = Store.open(":memory:", registry=services_registry())
        hub = Hub()
    else:
        store = Store.open(tmp_path / "s.db", registry=services_registry())
        hub = Hub(store=store)
    reg = ServiceRegistry.load(store, state=hub)
    yield type("Env", (), {"store": store, "hub": hub, "reg": reg, "tmp": tmp_path})
    store.close()


def run(coro):
    return asyncio.run(coro)


async def _ingest(env, app, sev=3, msg="boom", host="h"):
    ev = env.reg.tag(Event("syslog", host, app, sev, msg))
    return await env.hub.ingest(ev)


def test_statuses_lists_services_with_fields_and_unassigned_last(env):
    async def go():
        await env.reg.create_service(ServiceFields(id="a", name="A", app_names=("appa",)), actor="t")
        await env.reg.create_service_with_sources(
            ServiceFields(id="b", name="B", app_names=("appb",)),
            [LogSourceFields(path=str(env.tmp / "x.log"), format="plain")], actor="t")
        await _ingest(env, "appa", 2, "error one")
        await _ingest(env, "appb", 6, "fine")
        await _ingest(env, "other", 2, "error two")
    run(go())
    st = env.reg.statuses()
    assert [s.id for s in st] == ["a", "b", "unassigned"]
    a = st[0].to_dict()
    for key in ("last_seen", "state", "error_rate", "open_incidents", "events_total", "log_sources", "code_folder"):
        assert key in a
    assert a["events_total"] == 1 and a["open_incidents"] == 1 and a["state"] == "active"
    assert st[1].to_dict()["log_sources"][0]["status"] == "unknown"
    assert "lines_read" not in st[1].to_dict()["log_sources"][0]
    un = st[2]
    assert un.builtin is True and un.log_sources == () and un.code_folder is None and un.created_at is None
    assert un.open_incidents == 1 and un.events_total == 1 and un.name == "Unassigned"


def test_open_incidents_match_hub_view_and_ignore_resolved(env):
    async def go():
        await env.reg.create_service(ServiceFields(id="a", name="A", app_names=("appa",)), actor="t")
        await _ingest(env, "appa", 2, "error one")
        await _ingest(env, "appa", 2, "zebra crashed hard elsewhere")
        incs = env.hub.incidents(status="open", service_id="a")
        assert env.reg.status_of("a").open_incidents == len(incs) >= 1
        await env.hub.resolve(incs[0].id)
        assert env.reg.status_of("a").open_incidents == len(env.hub.incidents(status="open", service_id="a"))
    run(go())


def test_status_of(env):
    run(env.reg.create_service(ServiceFields(id="a", name="A"), actor="t"))
    assert env.reg.status_of("unassigned").builtin is True
    assert env.reg.status_of("nope") is None
    assert env.reg.status_of("a").state == "never_seen"


def _with_source(env):
    run(env.reg.create_service_with_sources(
        ServiceFields(id="a", name="A"),
        [LogSourceFields(path=str(env.tmp / "x.log"), format="plain"),
         LogSourceFields(path=str(env.tmp / "y.log"), format="plain")], actor="t"))


def test_provider_fills_runtime_fields_and_cuts_last_error(env):
    _with_source(env)
    calls = []

    def provider(sid):
        calls.append(sid)
        return SourceRuntime("running", 5, 4, 1, 99, 2, "e" * 500)

    env.reg.set_status_provider(provider)
    src = env.reg.statuses()[0].to_dict()["log_sources"]
    assert calls == ["ls1", "ls2"]
    assert src[0]["status"] == "running" and src[0]["lines_read"] == 5 and src[0]["events_emitted"] == 4
    assert src[0]["lines_dropped"] == 1 and src[0]["bytes_read"] == 99 and src[0]["unparsed"] == 2
    assert len(src[0]["last_error"]) == 200
    env.reg.set_status_provider(None)
    assert env.reg.statuses()[0].to_dict()["log_sources"][0]["status"] == "unknown"


def test_provider_that_raises_or_returns_wrong_type_gives_unknown_for_that_source(env, caplog):
    _with_source(env)

    def provider(sid):
        if sid == "ls1":
            raise RuntimeError("boom")
        return SourceRuntime("running", 1)

    env.reg.set_status_provider(provider)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.services"):
        src = env.reg.statuses()[0].to_dict()["log_sources"]
        env.reg.statuses()
    assert src[0]["status"] == "unknown" and "lines_read" not in src[0]
    assert src[1]["status"] == "running"
    assert len([r for r in caplog.records if "status provider" in r.getMessage()]) == 1   # once per minute
    env.reg.set_status_provider(lambda sid: {"status": "running"})
    assert env.reg.statuses()[0].to_dict()["log_sources"][1]["status"] == "unknown"


@pytest.mark.skipif(SKIP_BENCH, reason="benchmark")
def test_statuses_benchmark_100_services_100k_events_2000_incidents(tmp_path):
    store = Store.open(tmp_path / "b.db", registry=services_registry())
    hub = Hub()
    reg = ServiceRegistry.load(store, state=hub)

    async def create():
        for i in range(100):
            await reg.create_service(ServiceFields(id=f"s{i}", name=f"S{i}"), actor="t")
    run(create())

    def put(tx):
        rows = [(i, 1.0, float(i), "api", "h", "a", 6, "m", "", f"s{i % 100}", None) for i in range(1, 100_001)]
        tx.executemany(
            "INSERT INTO events (id, ts, ingested, source, host, app, severity, message, raw, service_id, log_source_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    store.transact_sync(put)

    class FakeIncident:
        def __init__(self, sid):
            self.service_id = sid
    fakes = [FakeIncident(f"s{i % 100}") for i in range(2000)]
    hub.incidents = lambda **kw: list(fakes)          # the view the registry reads
    reg._store = type("NoSql", (), {"read": None, "read_sync": None, "transact": None})()  # any SQLite call raises
    start = time.perf_counter()
    st = reg.statuses()
    assert time.perf_counter() - start < 0.05
    assert len(st) == 101 and st[0].open_incidents == 20
    store.close()
