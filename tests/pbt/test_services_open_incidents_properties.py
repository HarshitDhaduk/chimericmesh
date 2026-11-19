"""Property 12: open incidents in the status equal the hub view, on a plain and a store-backed hub."""
from __future__ import annotations
import asyncio
import tempfile
from collections import Counter
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store

APPS = ["alpha", "beta", "gamma", "stray"]
op = st.one_of(
    st.tuples(st.just("event"), st.sampled_from(APPS), st.integers(0, 3), st.integers(0, 4)),
    st.tuples(st.just("resolve"), st.integers(0, 10), st.just(0), st.just(0)),
    st.tuples(st.just("tick"), st.integers(1, 400), st.just(0), st.just(0)),
)


async def _play(hub: Hub, reg: ServiceRegistry, clock: list, ops) -> None:
    for sid, app in (("alpha", "alpha"), ("beta", "beta"), ("gamma", "gamma")):
        await reg.create_service(ServiceFields(id=sid, name=sid, app_names=(app,)), actor="t")
    for kind, a, b, c in ops:
        if kind == "event":
            await hub.ingest(reg.tag(Event("syslog", "h", a, b, f"message number {c} for {a}")))
        elif kind == "resolve":
            found = hub.incidents(status="open")
            if found:
                await hub.resolve(found[a % len(found)].id)
        else:
            clock[0] += a
        statuses = reg.statuses()
        expected = Counter(i.service_id or "unassigned" for i in hub.incidents(status="open"))
        for s in statuses:
            assert s.open_incidents == expected.get(s.id, 0)
            assert s.open_incidents == len(hub.incidents(status="open", service_id=s.id))
        assert sum(s.open_incidents for s in statuses) == len(hub.incidents(status="open"))


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=st.lists(op, max_size=25), backed=st.booleans())
def test_open_incidents_match_view(ops, backed):
    clock = [1000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(Path(tmp) / "s.db" if backed else ":memory:", registry=services_registry())
        try:
            hub = Hub(store=store, clock=lambda: clock[0]) if backed else Hub(clock=lambda: clock[0])
            reg = ServiceRegistry.load(store, state=hub, clock=lambda: clock[0])
            asyncio.run(_play(hub, reg, clock, ops))
        finally:
            store.close()
