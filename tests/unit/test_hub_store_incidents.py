"""Store-backed hub: incidents survive restarts, are persisted at the right moments, and rehydrate."""
import asyncio

import pytest

import chimericmesh.diagnostics as diagnostics
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.incidents import get_incident_row, load_incidents
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def ev(host="db01", msg=MSG, sev=3):
    return Event("syslog", host, "pg", sev, msg, msg, 1.0)


def open_store(path, **cfg):
    return Store.open(path, registry=core_registry(), config=StoreConfig.from_env({}, **cfg))


def make(store, clock=None):
    hub = Hub(store=store, clock=clock or Clock())
    said: list[str] = []

    async def fake(channel, text):
        said.append(text)

    hub.irc_broadcast = fake
    return hub, said


async def row(store, iid):
    return await store.read(lambda tx: get_incident_row(tx, iid))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "inc.db"


def test_count_continues_across_a_restart_without_a_new_announcement(db):
    async def go():
        s = open_store(db)
        hub, said = make(s)
        for _ in range(50):
            await hub.ingest(ev())
        iid = hub.incidents()[0].id
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub, said = make(s)
        assert hub.get_incident(iid).count == 50
        for _ in range(5):
            await hub.ingest(ev())
        assert hub.get_incident(iid).count == 55 and said == []
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_resolved_stays_resolved(db):
    async def go():
        s = open_store(db)
        hub, _ = make(s)
        for _ in range(3):
            await hub.ingest(ev())
        iid = hub.incidents()[0].id
        await hub.resolve(iid)
        assert (await row(s, iid)).resolved  # persisted on resolve
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub, _ = make(s)
        assert hub.get_incident(iid).resolved and iid in hub.resolved
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_persisted_at_open_and_milestones(db):
    async def go():
        s = open_store(db)
        hub, said = make(s)
        for _ in range(3):
            await hub.ingest(ev())
        iid = hub.incidents()[0].id
        r = await row(s, iid)
        assert r.count == 3 and r.announced_count == 3  # committed before the announcement returned
        for _ in range(7):
            await hub.ingest(ev())
        assert (await row(s, iid)).count == 10
        for _ in range(5):
            await hub.ingest(ev())
        assert (await row(s, iid)).count == 10  # no milestone: written behind
        await hub.aclose()
        assert (await row(s, iid)).count == 15  # aclose flushed it
        s.close()

    asyncio.run(go())


def test_flush_loop_writes_a_quiet_incident(db):
    async def go():
        clock = Clock()
        s = open_store(db, flush_tick=0.02, flush_interval=30.0)
        hub, _ = make(s, clock)
        hub.start()
        for _ in range(5):
            await hub.ingest(ev())
        iid = hub.incidents()[0].id
        assert (await row(s, iid)).count == 3
        clock.t += 31
        for _ in range(50):
            await asyncio.sleep(0.02)
            if (await row(s, iid)).count == 5:
                break
        assert (await row(s, iid)).count == 5
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_announce_comes_after_the_commit(db):
    async def go():
        s = open_store(db)
        hub = Hub(store=s, clock=Clock())
        seen = []

        async def fake(channel, text):
            if text.startswith("["):
                seen.append(await row(s, hub.incidents()[0].id))

        hub.irc_broadcast = fake
        for _ in range(3):
            await hub.ingest(ev())
        assert seen and seen[0] is not None and seen[0].count == 3
        await hub.aclose()
        s.close()

    asyncio.run(go())


class BrokenStore:
    """Wraps a store; every awaited write raises."""

    def __init__(self, real):
        self._real = real
        self.events, self.audit, self.config = real.events, real.audit, real.config

    def health(self):
        return self._real.health()

    async def transact(self, fn):
        raise OSError("disk gone")

    async def read(self, fn):
        return await self._real.read(fn)

    async def note_last_event_id(self, n):
        raise OSError("disk gone")


def test_store_failure_never_breaks_ingest_or_resolve(db):
    async def go():
        s = open_store(db)
        hub, said = make(s)
        hub.store = BrokenStore(s)
        hub.lifecycle.store = hub.store
        for _ in range(3):
            await hub.ingest(ev())
        inc = hub.incidents()[0]
        assert any(t.startswith("[") for t in said)  # announced anyway
        assert inc.id in hub.lifecycle._dirty
        assert (await hub.resolve(inc.id)).resolved
        hub.store = s
        hub.lifecycle.store = s
        await hub.lifecycle.flush_all()
        assert (await row(s, inc.id)).resolved  # retried
        await hub.aclose()
        s.close()

    asyncio.run(go())


class HangingStore(BrokenStore):
    async def transact(self, fn):
        await asyncio.get_running_loop().create_future()


def test_stalled_store_leaves_at_most_256_pending_persists(db):
    async def go():
        s = open_store(db)
        hub, _ = make(s)
        hub.store = HangingStore(s)
        hub.lifecycle.store = hub.store
        tasks = [asyncio.ensure_future(hub.ingest(ev(host=f"h{n}", sev=2))) for n in range(5000)]
        await asyncio.sleep(0.2)
        pending = [t for t in tasks if not t.done()]
        assert len(pending) <= 256
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        hub.store = s
        hub.lifecycle.store = None
        s.close()

    asyncio.run(go())


def test_evicted_incident_row_stays_and_rehydrates(db, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 3)

    async def go():
        s = open_store(db)
        hub, said = make(s)
        for _ in range(4):
            await hub.ingest(ev(host="first", sev=2))
        first = hub.incidents()[0].id
        for n in range(4):
            await hub.ingest(ev(host=f"other{n}", sev=2))
        assert hub.get_incident(first) is None and first in hub._stored_unloaded_ids
        await asyncio.sleep(0.05)
        assert (await row(s, first)).count == 4
        before = len([t for t in said if "first" in t])
        await hub.ingest(ev(host="first", sev=2))
        assert hub.get_incident(first).count == 5
        assert len([t for t in said if "first" in t]) == before  # not announced again
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_restart_loads_the_newest_max_clusters(db, monkeypatch):
    async def seed():
        s = open_store(db)
        clock = Clock()
        hub, _ = make(s, clock)
        for n in range(6):
            await hub.ingest(Event("syslog", f"host{n}", "a", 2, MSG, MSG, float(n)))
        ids = {i.host: i.id for i in hub.incidents()}
        for _ in range(3):  # host0 gets 4 events in total
            await hub.ingest(Event("syslog", "host0", "a", 2, MSG, MSG, 0.0))
        await hub.resolve(ids["host0"])
        await hub.aclose()
        s.close()
        return ids

    ids = asyncio.run(seed())
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 3)  # after import: the hub reads it at call time (L8)

    async def go():
        s = open_store(db)
        hub, said = make(s)
        assert {i.host for i in hub.incidents()} == {"host3", "host4", "host5"}
        assert hub._stored_unloaded_ids == {ids["host0"], ids["host1"], ids["host2"]}
        await hub.ingest(Event("syslog", "host0", "a", 2, MSG, MSG, 0.0))
        inc = hub.get_incident(ids["host0"])
        assert inc.count == 5 and inc.resolved and inc.opening == 1
        assert said == [] and hub._announced[inc.id] == 1
        assert (await row(s, ids["host0"])).count >= 4  # never lowered
        await hub.aclose()
        assert (await row(s, ids["host0"])).count == 5
        s.close()

    asyncio.run(go())


def test_skipped_persist_then_flush_then_restart_does_not_reannounce(db):
    async def go():
        s = open_store(db, persist_slots=1)
        hub, said = make(s)
        hub.lifecycle._slots = asyncio.Semaphore(0)  # every persist is skipped
        for _ in range(3):
            await hub.ingest(ev())
        iid = hub.incidents()[0].id
        assert await row(s, iid) is None and iid in hub.lifecycle._dirty
        hub.lifecycle._slots = asyncio.Semaphore(1)
        await hub.lifecycle.flush_all()
        assert (await row(s, iid)).announced_count == 3
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub, said = make(s)
        await hub.ingest(ev())
        assert said == []
        await hub.aclose()
        s.close()

    asyncio.run(go())
