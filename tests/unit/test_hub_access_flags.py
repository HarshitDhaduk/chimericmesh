"""Hub flags (``enforce_access``, ``access``, ``tagger``) and the tagging hook in ``ingest`` (B12, R4.4, R4.5)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store import Store
from chimericmesh.store.schema import core_registry

MSG = "could not write block 1 No space left on device"


def ev(host="db01", message=MSG, **kw):
    return Event(source="syslog", host=host, app="pg", severity=3, message=message, **kw)


@pytest.fixture(params=["plain", "store"])
def hub(request, tmp_path):
    if request.param == "plain":
        yield Hub()
        return
    store = Store.open(tmp_path / "h.db", registry=core_registry())
    h = Hub(store=store)
    yield h
    asyncio.run(h.aclose())
    store.close()


def tag_as(service_id):
    return lambda e: dataclasses.replace(e, service_id=service_id)


def test_defaults_and_flag(hub):
    assert hub.enforce_access is False and hub.access is None and hub.tagger is None
    assert Hub(enforce_access=True).enforce_access is True


def test_tagger_result_reaches_ring_and_return_value(hub):
    hub.tagger = tag_as("s1")
    out = asyncio.run(hub.ingest(ev()))
    assert out.service_id == "s1"
    assert hub.recent(5)[-1].service_id == "s1"


def test_tagged_event_is_stored_and_incident_takes_the_service(tmp_path):
    store = Store.open(tmp_path / "t.db", registry=core_registry())
    hub = Hub(store=store)
    hub.tagger = tag_as("s1")

    async def go():
        for _ in range(3):
            await hub.ingest(ev())
        stored = await hub.events_after(0, 10)
        await hub.aclose()
        return stored

    stored = asyncio.run(go())
    assert [e.service_id for e in stored] == ["s1"] * 3
    assert [i.service_id for i in hub.incidents()] == ["s1"]
    store.close()


def test_incident_of_three_tagged_events_has_the_service_in_memory():
    hub = Hub()
    hub.tagger = tag_as("s1")

    async def go():
        for _ in range(3):
            await hub.ingest(ev())
    asyncio.run(go())
    assert [i.service_id for i in hub.incidents()] == ["s1"]


def test_failing_tagger_ingests_untagged_and_logs_once_a_minute(hub, caplog):
    def boom(e):
        raise RuntimeError("tagger bug")
    hub.tagger = boom
    with caplog.at_level(logging.ERROR, logger="chimericmesh.hub"):
        out = [asyncio.run(hub.ingest(ev())) for _ in range(3)]
    assert all(o.service_id is None for o in out)
    assert len(hub.recent(10)) == 3
    assert sum("tagger failed" in r.getMessage() for r in caplog.records) == 1


def test_tagger_runs_before_id_and_before_tracker():
    hub = Hub()
    order = []
    seen = {}
    original_add = hub.tracker.add

    def spy_tag(e):
        seen["id_at_tag"] = e.id
        order.append("tag")
        return dataclasses.replace(e, service_id="s1")

    def spy_add(e):
        order.append("tracker")
        seen["svc_at_tracker"] = e.service_id
        return original_add(e)

    hub.tagger = spy_tag
    hub.tracker.add = spy_add
    asyncio.run(hub.ingest(ev()))
    assert order == ["tag", "tracker"]
    assert seen["id_at_tag"] in (0, None)
    assert seen["svc_at_tracker"] == "s1"


def test_incident_without_service_adopts_a_later_one_once_and_keeps_it():
    hub = Hub()
    assigned = []
    hub.tagger = lambda e: dataclasses.replace(e, service_id=assigned.pop(0))

    async def go():
        for sid in (None, "s1", "s2"):
            assigned.append(sid)
            await hub.ingest(ev())
    asyncio.run(go())
    assert [i.service_id for i in hub.incidents()] == ["s1"]


def test_incident_whose_first_event_had_a_service_never_changes_it():
    hub = Hub()
    assigned = ["s1", None, "s2"]
    hub.tagger = lambda e: dataclasses.replace(e, service_id=assigned.pop(0))

    async def go():
        for _ in range(3):
            await hub.ingest(ev())
    asyncio.run(go())
    assert [i.service_id for i in hub.incidents()] == ["s1"]


def test_incident_that_never_gets_a_tagged_event_stays_unassigned():
    hub = Hub()
    for _ in range(3):
        asyncio.run(hub.ingest(ev()))
    assert [i.service_id for i in hub.incidents()] == [None]


def test_a_registry_tag_ignores_a_preset_service_id(svc_mem_store):
    from chimericmesh.services.registry import ServiceRegistry
    hub = Hub()
    registry = ServiceRegistry.load(svc_mem_store, state=hub)
    hub.tagger = registry.tag
    out = asyncio.run(hub.ingest(ev(service_id="ghost")))
    assert out.service_id is None
