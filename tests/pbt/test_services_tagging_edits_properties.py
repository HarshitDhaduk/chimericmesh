"""Property 9: tagging ignores later registry changes for stored events."""
from __future__ import annotations

import asyncio
import os
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.errors import ServicesError
from chimericmesh.services.models import ServiceFields, ServicePatch
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store

HOSTS = ["h1", "h2", "h3"]


@st.composite
def steps(draw):
    out = []
    for _ in range(draw(st.integers(1, 12))):
        kind = draw(st.sampled_from(["event", "create", "hosts", "delete"]))
        out.append((kind, draw(st.sampled_from(["a", "b"])), draw(st.sampled_from(HOSTS)),
                    draw(st.lists(st.sampled_from(HOSTS), max_size=2, unique=True))))
    return out


@settings(max_examples=30, deadline=None)
@given(steps())
def test_property_9_stored_events_keep_their_service(plan):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(os.path.join(tmp, "t.db"), registry=services_registry())
        try:
            reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
            model: dict[str, list[str]] = {}
            order: list[str] = []
            tagged: list[tuple[Event, str | None]] = []

            def expected(host):
                for sid in order:
                    if host in model[sid]:
                        return sid
                return None

            async def go():
                for kind, sid, host, hosts in plan:
                    try:
                        if kind == "event":
                            ev = reg.tag(Event("syslog", host, "app", 3, "m"))
                            assert ev.service_id == expected(host)
                            tagged.append((ev, ev.service_id))
                            store.transact_sync(lambda tx, s=ev.service_id: tx.execute(
                                "INSERT INTO events (ts, source, host, app, severity, message, service_id, ingested)"
                                " VALUES (0,'s','h','a',3,'m',?,0)", (s,)), allow_blocking=True)
                        elif kind == "create":
                            await reg.create_service(ServiceFields(id=sid, name=sid, hosts=tuple(hosts)), actor="t")
                            model[sid] = list(hosts)
                            order.append(sid)
                        elif kind == "hosts":
                            await reg.update_service(sid, ServicePatch(hosts=tuple(hosts)), actor="t")
                            model[sid] = list(hosts)
                        else:
                            await reg.delete_service(sid, actor="t")
                            model.pop(sid, None)
                            order.remove(sid)
                    except ServicesError:
                        pass
                    # events already tagged keep their id (immutable); new events follow the current rules
                    for ev, sid_at_tag in tagged:
                        assert ev.service_id == sid_at_tag
                    for h in HOSTS:
                        assert reg.tag(Event("syslog", h, "app", 3, "m")).service_id == expected(h)

            asyncio.run(go())
            rows = store.read_sync(lambda tx: [r[0] for r in tx.all("SELECT service_id FROM events ORDER BY id")],
                                   allow_blocking=True)
            assert rows == [sid for _, sid in tagged]
        finally:
            store.close()
