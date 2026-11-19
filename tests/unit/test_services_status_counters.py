"""Counters, ``state`` and ``error_rate`` (R4.4, R5.1 to R5.3)."""
from __future__ import annotations
import asyncio
import os
import threading
import time

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.services.status import ERROR_WINDOW, StatsTracker, compute_state, error_rate
from chimericmesh.store import Store

SKIP_BENCH = os.environ.get("CHIMERICMESH_SKIP_BENCH") == "1"


def test_state_never_seen_active_idle():
    assert compute_state(None, 1000.0) == "never_seen"
    assert compute_state(700.0, 1000.0) == "active"   # 300 s old
    assert compute_state(701.0, 1000.0) == "active"   # 299 s old
    assert compute_state(699.0, 1000.0) == "idle"     # 301 s old
    assert compute_state(2000.0, 1000.0) == "active"  # future last_seen


@pytest.mark.parametrize("errors,rate", [(0, 0.0), (4, 0.8), (5, 1.0), (6, 1.2)])
def test_error_rate(errors, rate):
    assert error_rate(errors) == rate


def test_only_severity_0_to_3_counts():
    t = StatsTracker()
    for sev in range(8):
        t.observe("a", sev, 100.0)
    snap = t.snapshot("a", 100.0)
    assert snap.errors_in_window == 4 and snap.events_total == 8


def test_window_boundary_in_whole_seconds():
    t = StatsTracker()
    t.observe("a", 3, 100.0)
    assert t.snapshot("a", 399.0).errors_in_window == 1
    assert t.snapshot("a", 400.0).errors_in_window == 0
    assert ERROR_WINDOW == 300


def test_observe_updates_total_and_last_seen():
    t = StatsTracker()
    assert t.snapshot("a", 1.0).last_seen is None
    t.observe("a", 6, 10.0)
    t.observe("a", 6, 12.5)
    snap = t.snapshot("a", 13.0)
    assert snap.events_total == 2 and snap.last_seen == 12.5


def test_none_service_is_unassigned():
    t = StatsTracker()
    t.observe(None, 2, 5.0)
    assert t.snapshot("unassigned", 5.0).events_total == 1
    assert t.snapshot(None, 5.0).events_total == 1


def test_seed_and_drop():
    t = StatsTracker()
    t.seed([("a", 7, 50.0), ("unassigned", 2, 60.0)])
    assert t.snapshot("a", 100.0).events_total == 7 and t.snapshot("a", 100.0).last_seen == 50.0
    t.drop("a")
    assert t.snapshot("a", 100.0).events_total == 0 and t.snapshot("a", 100.0).last_seen is None


def _reg(store, clock=None):
    return ServiceRegistry.load(store, state=Hub(), clock=clock)


def test_tag_observes_once_and_changes_only_service_id(svc_mem_store):
    now = [1000.0]
    reg = _reg(svc_mem_store, lambda: now[0])
    asyncio.run(reg.create_service(ServiceFields(id="web", name="Web", app_names=("nginx",)), actor="t"))
    ev = Event("syslog", "h", "nginx", 3, "boom")
    out = reg.tag(ev)
    assert out.service_id == "web"
    assert out.message == ev.message and out.severity == ev.severity and out.app == ev.app
    other = reg.tag(Event("syslog", "h", "zzz", 6, "x"))
    assert other.service_id is None
    st = reg.status_of("web")
    assert st.events_total == 1 and st.last_seen == 1000.0 and st.error_rate == 0.2
    assert reg.status_of("unassigned").events_total == 1


def test_load_seeds_counters_from_stored_events(tmp_path):
    path = tmp_path / "s.db"
    store = Store.open(path, registry=services_registry())
    reg = _reg(store)
    asyncio.run(reg.create_service(ServiceFields(id="web", name="Web"), actor="t"))
    asyncio.run(reg.create_service(ServiceFields(id="idle", name="Idle"), actor="t"))
    asyncio.run(reg.create_service(ServiceFields(id="none", name="None"), actor="t"))

    def put(tx):
        for i, (sid, ing) in enumerate([("web", 10.0), ("web", 20.0), (None, 30.0)], 1):
            tx.execute(
                "INSERT INTO events (id, ts, ingested, source, host, app, severity, message, raw, service_id, log_source_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (i, ing, ing, "api", "h", "a", 6, "m", "", sid, None))
    store.transact_sync(put)
    store.close()
    store = Store.open(path, registry=services_registry())
    reg = ServiceRegistry.load(store, state=Hub(), clock=lambda: 10_000.0)
    web = reg.status_of("web")
    assert web.events_total == 2 and web.last_seen == 20.0 and web.state == "idle"
    assert reg.status_of("none").state == "never_seen" and reg.status_of("none").events_total == 0
    assert reg.status_of("unassigned").events_total == 1 and reg.status_of("unassigned").last_seen == 30.0
    store.close()


def test_plain_memory_hub_starts_at_zero(svc_mem_store):
    reg = _reg(svc_mem_store)
    assert reg.status_of("unassigned").events_total == 0


def test_delete_drops_counters(svc_mem_store):
    reg = _reg(svc_mem_store, lambda: 5.0)
    asyncio.run(reg.create_service(ServiceFields(id="web", name="Web", app_names=("n",)), actor="t"))
    reg.tag(Event("syslog", "h", "n", 3, "m"))
    asyncio.run(reg.delete_service("web", actor="t"))
    assert reg._stats.snapshot("web", 5.0).events_total == 0


def test_no_task_or_thread_created(svc_mem_store):
    async def go():
        await svc_mem_store.transact(lambda tx: None)   # the store's own writer thread starts lazily
        before_tasks = len(asyncio.all_tasks())
        before_threads = threading.active_count()
        reg = _reg(svc_mem_store)
        await reg.create_service(ServiceFields(id="web", name="Web", app_names=("n",)), actor="t")
        reg.tag(Event("syslog", "h", "n", 3, "m"))
        await reg.delete_service("web", actor="t")
        assert len(asyncio.all_tasks()) == before_tasks
        assert threading.active_count() == before_threads
    asyncio.run(go())


@pytest.mark.skipif(SKIP_BENCH, reason="benchmark")
def test_seed_100k_rows_under_500ms(tmp_path):
    store = Store.open(tmp_path / "b.db", registry=services_registry())

    def put(tx):
        rows = [(i, 1.0, float(i), "api", "h", "a", 6, "m", "", f"s{i % 100}", None) for i in range(1, 100_001)]
        tx.executemany(
            "INSERT INTO events (id, ts, ingested, source, host, app, severity, message, raw, service_id, log_source_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    store.transact_sync(put)
    hub = Hub()
    start = time.perf_counter()
    ServiceRegistry.load(store, state=hub)
    assert time.perf_counter() - start < 0.5
    store.close()
