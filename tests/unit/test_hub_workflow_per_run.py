"""Guided-workflow flags are per run: only an action taken in this process marks a step done."""
import asyncio

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub

MSG = "could not write block 1 No space left on device"


def ev():
    return Event(source="syslog", host="db01", app="pg", severity=3, message=MSG)


def done(hub):
    return [s["key"] for s in hub.workflow()["steps"] if s["done"]]


def test_fresh_hub_has_nothing_done():
    assert done(Hub()) == []


def test_each_flag_turns_on_only_after_its_action():
    async def go():
        hub = Hub()
        await hub.ingest(ev())
        assert done(hub) == ["traffic"]
        await hub.ingest(ev())
        await hub.ingest(ev())
        assert done(hub) == ["traffic", "incidents"]
        inc = hub.incidents()[0]
        prop = await hub.create_proposal(inc.id, "fix it")
        assert done(hub) == ["traffic", "incidents", "propose"]
        await hub.decide(prop.id, True, "alice")
        assert "decide" in done(hub)
        await hub.resolve(inc.id)
        assert done(hub) == ["traffic", "incidents", "propose", "decide", "resolve"]

    asyncio.run(go())


def test_a_reopening_counts_as_an_opening_in_this_run():
    async def go():
        t = {"now": 100.0}
        hub = Hub(clock=lambda: t["now"], reopen_after=0)
        for _ in range(3):
            await hub.ingest(ev())
        assert hub._run_opened == 1
        await hub.resolve(hub.incidents()[0].id)
        await hub.ingest(ev())
        assert hub._run_opened == 2

    asyncio.run(go())


def test_persisted_state_does_not_mark_steps_done(tmp_path):
    from chimericmesh.store.incidents import IncidentRow, upsert_incident
    from chimericmesh.store.proposals import create_proposal
    from chimericmesh.store.schema import core_registry
    from chimericmesh.store.store import Store
    from chimericmesh.diagnostics import Incident

    store = Store.open(tmp_path / "w.db", registry=core_registry())
    inc = Incident(id="abc1234567", host="db01", category="disk", signature="no space", severity=3, count=9,
                   first_seen=1.0, last_seen=2.0, sample=MSG, opened=True, sources=["syslog"])
    store.transact_sync(lambda tx: upsert_incident(tx, IncidentRow.from_incident(inc, 9)))
    store.transact_sync(lambda tx: create_proposal(tx, incident_id=inc.id, action="a", details="", service_id=None,
                                                   incident_opening=1, now=1.0))
    store.close()
    store = Store.open(tmp_path / "w.db", registry=core_registry())
    try:
        hub = Hub(store=store)  # type: ignore[call-arg]
        assert hub.get_incident(inc.id) is not None and hub.get_proposal("p1") is not None
        assert done(hub) == []
    finally:
        store.close()
