"""Rehydrate on miss: one shared read per id, no race, no partial cluster after a failed read."""
import asyncio

import pytest

import chimericmesh.diagnostics as diagnostics
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.errors import StoreError
from chimericmesh.store.incidents import get_incident_row
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def ev(host):
    return Event("syslog", host, "pg", 2, MSG, MSG, 1.0)


class HeldReads:
    """Wraps the store: reads can be held and released, or made to fail once."""

    def __init__(self, real):
        self._real = real
        self.events, self.audit, self.config = real.events, real.audit, real.config
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail_next = False
        self.reads = 0

    def health(self):
        return self._real.health()

    async def transact(self, fn):
        return await self._real.transact(fn)

    async def note_last_event_id(self, n):
        return await self._real.note_last_event_id(n)

    async def read(self, fn):
        self.reads += 1
        await self.gate.wait()
        if self.fail_next:
            self.fail_next = False
            raise StoreError("read failed")
        return await self._real.read(fn)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A stored incident for host 'old' (count 4, announced) that is not loaded after a restart."""
    path = tmp_path / "r.db"

    async def seed():
        s = Store.open(path, registry=core_registry())
        hub = Hub(store=s, clock=Clock())
        for _ in range(4):
            await hub.ingest(ev("old"))
        iid = hub.incidents()[0].id
        for n in range(3):
            await hub.ingest(Event("syslog", f"new{n}", "pg", 2, MSG, MSG, 5.0 + n))
        await hub.aclose()
        s.close()
        return iid

    iid = asyncio.run(seed())
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 3)
    return path, iid


def open_hub(path, clock=None):
    s = Store.open(path, registry=core_registry())
    hub = Hub(store=s, clock=clock or Clock())
    said: list[str] = []

    async def fake(channel, text):
        said.append(text)

    hub.irc_broadcast = fake
    held = HeldReads(s)
    hub.store = held
    hub.lifecycle.store = held
    return s, hub, held, said


async def finish(s, hub):
    hub.store = s
    hub.lifecycle.store = s
    await hub.aclose()
    s.close()


def spy_install(hub):
    calls = []
    original = hub.tracker.install

    def install(inc):
        calls.append(inc.id)
        original(inc)

    hub.tracker.install = install
    return calls


def test_concurrent_events_share_one_read_and_one_install(setup):
    path, iid = setup

    async def go():
        s, hub, held, _ = open_hub(path)
        assert iid in hub._stored_unloaded_ids
        calls = spy_install(hub)
        held.gate.clear()
        tasks = [asyncio.ensure_future(hub.ingest(ev("old"))) for _ in range(2)]
        await asyncio.sleep(0.02)
        held.gate.set()
        await asyncio.gather(*tasks)
        assert calls == [iid] and held.reads == 1
        assert hub.get_incident(iid).count == 6 and iid not in hub._stored_unloaded_ids
        await finish(s, hub)

    asyncio.run(go())


def test_resolved_unloaded_incident_reopens_once(tmp_path, monkeypatch):
    path = tmp_path / "rr.db"
    clock = Clock()

    async def seed():
        s = Store.open(path, registry=core_registry())
        hub = Hub(store=s, clock=clock)
        for _ in range(3):
            await hub.ingest(ev("old"))
        iid = hub.incidents()[0].id
        await hub.resolve(iid)
        for n in range(3):
            await hub.ingest(Event("syslog", f"new{n}", "pg", 2, MSG, MSG, 5.0 + n))
        await hub.aclose()
        s.close()
        return iid

    iid = asyncio.run(seed())
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 3)
    clock.t += 1000

    async def go():
        s, hub, held, said = open_hub(path, clock)
        held.gate.clear()
        tasks = [asyncio.ensure_future(hub.ingest(ev("old"))) for _ in range(2)]
        await asyncio.sleep(0.02)
        held.gate.set()
        await asyncio.gather(*tasks)
        inc = hub.get_incident(iid)
        assert inc.opening == 2 and not inc.resolved
        assert len([t for t in said if t.startswith("REOPENED")]) == 1
        await finish(s, hub)

    asyncio.run(go())


def test_failed_read_creates_no_cluster_and_leaves_the_row(setup):
    path, iid = setup

    async def go():
        s, hub, held, said = open_hub(path)
        before = await s.read(lambda tx: get_incident_row(tx, iid))
        held.fail_next = True
        e = await hub.ingest(ev("old"))
        assert hub.events[-1] is e and not hub.tracker.known(iid)
        assert iid in hub._stored_unloaded_ids and said == []
        await hub.lifecycle.flush_dirty()
        await s.events.barrier()
        after = await s.read(lambda tx: get_incident_row(tx, iid))
        assert (after.count, after.opening, after.announced_count) == (
            before.count, before.opening, before.announced_count)
        assert [x.id for x in await s.events.after(e.id - 1, 5)] == [e.id]  # the event row is queued
        await hub.ingest(ev("old"))  # a working read continues the stored count
        assert hub.get_incident(iid).count == before.count + 1
        await finish(s, hub)

    asyncio.run(go())


def test_cancelling_one_waiter_lets_the_other_finish(setup):
    path, iid = setup

    async def go():
        s, hub, held, _ = open_hub(path)
        calls = spy_install(hub)
        held.gate.clear()
        a = asyncio.ensure_future(hub.ingest(ev("old")))
        b = asyncio.ensure_future(hub.ingest(ev("old")))
        await asyncio.sleep(0.02)
        a.cancel()
        held.gate.set()
        await asyncio.gather(a, b, return_exceptions=True)
        assert calls == [iid] and hub._rehydrating == {}
        assert hub.get_incident(iid).count == 5
        await finish(s, hub)

    asyncio.run(go())
