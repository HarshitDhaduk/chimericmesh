"""Store-backed hub: startup load, event ids and counters across restarts, events_after stitching."""
import asyncio
import threading

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


def ev(n=0, source="syslog", sev=6, service=None):
    return Event(source, "h1", "app", sev, f"hello {n}", f"raw {n}", 1.0 + n, service_id=service)


def open_store(path, **cfg):
    return Store.open(path, registry=core_registry(), config=StoreConfig.from_env({}, **cfg))


async def ingest_n(hub, n, start=0, **kw):
    return [await hub.ingest(ev(start + i, **kw)) for i in range(n)]


async def drain(hub):
    await hub.store.events.barrier()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hub.db"


def test_bare_hub_has_no_store():
    assert Hub().store is None


def test_restart_reloads_ring_ids_and_totals(db):
    async def first():
        s = open_store(db)
        hub = Hub(store=s)
        assert hub.store is s
        await ingest_n(hub, 5)
        await ingest_n(hub, 2, start=10, source="irc")
        await drain(hub)
        await hub.aclose()
        s.close()

    asyncio.run(first())

    async def second():
        s = open_store(db)
        hub = Hub(store=s, capacity=4)
        assert [e.id for e in hub.events] == [4, 5, 6, 7]
        assert hub.total_events == 7 and hub.total_by_source == {"syslog": 5, "irc": 2}
        assert (await hub.ingest(ev(99))).id == 8
        assert hub.transcript == type(hub.transcript)(maxlen=hub.transcript.maxlen)  # empty, R9.1
        await hub.aclose()
        s.close()

    asyncio.run(second())


def test_ingest_queues_rows(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s)
        await ingest_n(hub, 3)
        await drain(hub)
        rows = await s.events.after(0, 10)
        assert [e.id for e in rows] == [1, 2, 3] and rows[0].message == "hello 0"
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_ids_continue_after_retention_emptied_the_table(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s)
        await ingest_n(hub, 4)
        await drain(hub)
        await s.transact(lambda tx: tx.execute("DELETE FROM events"))
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub = Hub(store=s)
        assert (await hub.ingest(ev())).id == 5
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_ids_after_a_crash_are_above_every_id_shown(db):
    async def go():
        s = open_store(db, queue_limit=100, batch_rows=10)
        hub = Hub(store=s)
        shown = [e.id for e in await ingest_n(hub, 30)]
        s._abandon()
        s = open_store(db, queue_limit=100, batch_rows=10)
        assert not s.previous_shutdown_clean
        hub = Hub(store=s)
        nxt = (await hub.ingest(ev())).id
        assert nxt > max(shown) and nxt > 2 * 100 + 10
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_graceful_close_records_the_last_assigned_id_even_if_rows_were_dropped(db):
    async def go():
        s = open_store(db, batch_ms=60_000, batch_rows=10_000)
        hub = Hub(store=s)
        await ingest_n(hub, 5)  # still queued: batch_ms is a minute
        await hub.aclose()
        s._abandon()  # the queued rows are never written
        s = open_store(db)
        assert await s.read(lambda tx: tx.counter_get("event_id")) >= 5
        assert await s.read(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0]) == 0
        s.close()

    asyncio.run(go())


def test_ingest_after_aclose_updates_memory_and_writes_nothing(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s)
        await hub.aclose()
        e = await hub.ingest(ev(1, sev=3))
        assert e.id == 1 and len(hub.events) == 1
        s.close()
        s = open_store(db)
        assert await s.events.after(0, 10) == []
        s.close()

    asyncio.run(go())


def test_events_after_from_ring_db_and_stitch(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s, capacity=5)
        await ingest_n(hub, 12)
        ring_min = hub.events[0].id
        assert ring_min == 8
        # boundary: after_id == ring_min - 1 is served from the ring alone
        assert [e.id for e in await hub.events_after(ring_min - 1)] == [8, 9, 10, 11, 12]
        # lower after_id: database then ring, no gap and no duplicate (barrier runs first)
        assert [e.id for e in await hub.events_after(2)] == list(range(3, 13))
        assert [e.id for e in await hub.events_after(2, 4)] == [3, 4, 5, 6]
        assert [e.id for e in await hub.events_after(5, 4)] == [6, 7, 8, 9]
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_events_after_stitch_waits_for_queued_rows(db):
    async def go():
        s = open_store(db, batch_ms=60_000, batch_rows=100_000, queue_limit=20_000)
        hub = Hub(store=s, capacity=100)
        await ingest_n(hub, 15_000)
        got = await hub.events_after(0, 500)
        assert [e.id for e in got] == list(range(1, 501))
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_events_after_with_id_gaps_and_empty_ring(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s, capacity=3)
        await ingest_n(hub, 8)
        await drain(hub)
        await s.transact(lambda tx: tx.execute("DELETE FROM events WHERE id IN (2, 3)"))
        assert [e.id for e in await hub.events_after(0)] == [1, 4, 5, 6, 7, 8]
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub = Hub(store=s, capacity=3)
        hub.events.clear()  # as for a fresh store: no ring at all
        assert [e.id for e in await hub.events_after(5)] == [6, 7, 8]
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_events_after_filters_on_the_database_path(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s, capacity=2)
        for n in range(6):
            await hub.ingest(ev(n, source="irc" if n % 2 else "syslog", service="s1" if n < 3 else None))
        assert [e.id for e in await hub.events_after(0, source="irc")] == [2, 4, 6]
        assert [e.id for e in await hub.events_after(0, service_id="s1")] == [1, 2, 3]
        assert [e.id for e in await hub.events_after(0, service_id="unassigned")] == [4, 5, 6]
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_weird_source_does_not_poison_a_batch(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s)
        await hub.ingest(ev(1, source="Weird Source!"))
        await hub.ingest(ev(2))
        await drain(hub)
        assert len(await s.events.after(0, 10)) == 2 and s.health().store_ok
        await hub.aclose()
        s.close()

    asyncio.run(go())
