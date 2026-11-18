"""StateView filters on the hub, the REST parameters that expose them, and the stats keys."""
import asyncio

import httpx

from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.ports import StateView


def ev(msg, host="h1", sev=2, service=None, source="syslog", ts=1.0):
    return Event(source=source, host=host, app="a", severity=sev, message=msg, ts=ts, service_id=service)


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, url, **kw)

    return asyncio.run(run())


async def seeded():
    """Three incidents: svc1 (open), svc1 (resolved) and one without a service."""
    hub = Hub()
    await hub.ingest(ev("kernel panic alpha", host="a", service="svc1"))
    await hub.ingest(ev("kernel panic beta", host="b", service="svc1"))
    await hub.ingest(ev("kernel panic gamma", host="c"))
    beta = next(i for i in hub.incidents() if i.host == "b")
    await hub.resolve(beta.id)
    return hub


def test_hub_satisfies_the_state_view_protocol():
    hub = Hub()
    for name in ("incidents", "get_incident", "get_proposal", "list_proposals", "recent", "events_after"):
        assert callable(getattr(hub, name))
    assert StateView is not None


def test_incident_filters_apply_before_the_limit():
    async def go():
        hub = await seeded()
        assert len(hub.incidents()) == 3
        assert {i.host for i in hub.incidents(status="open")} == {"a", "c"}
        assert {i.host for i in hub.incidents(status="resolved")} == {"b"}
        assert {i.host for i in hub.incidents(service_id="svc1")} == {"a", "b"}
        assert {i.host for i in hub.incidents(service_id="unassigned")} == {"c"}
        assert len(hub.incidents(limit=2)) == 2
        # limit applies after the filter: one svc1 open incident is returned even with limit 1
        assert [i.host for i in hub.incidents(status="open", service_id="svc1", limit=1)] == ["a"]

    asyncio.run(go())


def test_list_proposals_filters_then_returns_the_newest_oldest_first():
    async def go():
        hub = await seeded()
        incs = {i.host: i for i in hub.incidents()}
        ids = []
        for host in ("a", "c", "a", "b"):
            ids.append((await hub.create_proposal(incs[host].id, f"fix {host}")).id)
        await hub.decide(ids[0], True, "x")
        assert [p.id for p in hub.list_proposals()] == ids
        assert [p.id for p in hub.list_proposals(limit=2)] == ids[-2:]
        assert [p.id for p in hub.list_proposals("pending", limit=2)] == ids[2:]
        assert [p.id for p in hub.list_proposals(service_id="svc1")] == [ids[0], ids[2], ids[3]]
        assert [p.id for p in hub.list_proposals(service_id="svc1", limit=2)] == ids[2:]
        assert [p.id for p in hub.list_proposals(service_id="unassigned")] == [ids[1]]

    asyncio.run(go())


def test_events_after_serves_from_the_ring_oldest_first_with_filters():
    async def go():
        hub = Hub()
        for n in range(6):
            await hub.ingest(ev(f"m{n}", sev=6, service="s1" if n % 2 else None, source="irc" if n < 3 else "syslog"))
        assert [e.id for e in await hub.events_after(0)] == [1, 2, 3, 4, 5, 6]
        assert [e.id for e in await hub.events_after(2, 2)] == [3, 4]
        assert [e.id for e in await hub.events_after(0, source="irc")] == [1, 2, 3]
        assert [e.id for e in await hub.events_after(0, service_id="s1")] == [2, 4, 6]
        assert [e.id for e in await hub.events_after(0, 2, service_id="s1")] == [2, 4]  # filter before limit
        assert [e.id for e in await hub.events_after(0, service_id="unassigned")] == [1, 3, 5]
        assert await hub.events_after(6) == []

    asyncio.run(go())


def test_rest_events_after_id_is_oldest_first_and_default_is_newest_first():
    async def go():
        hub = Hub()
        for n in range(4):
            await hub.ingest(ev(f"m{n}", sev=6))
        return hub

    hub = asyncio.run(go())
    app = create_app(hub, start_listeners=False)
    assert [e["id"] for e in call(app, "GET", "/api/events").json()] == [4, 3, 2, 1]
    assert [e["id"] for e in call(app, "GET", "/api/events?after_id=1").json()] == [2, 3, 4]
    assert [e["id"] for e in call(app, "GET", "/api/events?after_id=1&limit=2").json()] == [2, 3]
    assert call(app, "GET", "/api/events?after_id=-1").status_code == 422
    assert call(app, "GET", "/api/events?after_id=x").status_code == 422
    assert call(app, "GET", "/api/events?limit=0").status_code == 200
    assert len(call(app, "GET", "/api/events?limit=0").json()) == 1
    assert call(app, "GET", "/api/events?limit=9999").status_code == 200
    assert call(app, "GET", "/api/events?service_id=" + "x" * 65).status_code == 422


def test_rest_incident_and_proposal_filters_and_limits():
    hub = asyncio.run(seeded())
    app = create_app(hub, start_listeners=False)
    assert len(call(app, "GET", "/api/incidents").json()) == 3  # no default limit
    assert len(call(app, "GET", "/api/incidents?limit=1").json()) == 1
    assert {i["host"] for i in call(app, "GET", "/api/incidents?status=open&service_id=svc1").json()} == {"a"}
    assert len(call(app, "GET", "/api/incidents?service_id=unassigned").json()) == 1
    for bad in ("limit=0", "limit=501", "status=bogus", "service_id=" + "x" * 65):
        assert call(app, "GET", "/api/incidents?" + bad).status_code == 422, bad
    for bad in ("limit=0", "limit=501", "service_id=" + "x" * 65, "status=bogus"):
        assert call(app, "GET", "/api/proposals?" + bad).status_code == 422, bad
    assert call(app, "GET", "/api/proposals?limit=500").status_code == 200
    first = hub.incidents()[0]
    for n in range(3):
        call(app, "POST", "/api/proposals", json={"incident_id": first.id, "action": f"a{n}"})
    assert [p["id"] for p in call(app, "GET", "/api/proposals?limit=2").json()] == ["p2", "p3"]
    assert len(call(app, "GET", "/api/proposals").json()) == 3


def test_incident_dicts_carry_service_id_and_opening():
    hub = asyncio.run(seeded())
    app = create_app(hub, start_listeners=False)
    data = call(app, "GET", "/api/incidents").json()
    assert all({"service_id", "opening", "resolved_at"} <= set(d) and "opened" not in d for d in data)


def test_stats_has_store_keys():
    stats = Hub().stats()
    assert stats["store_ok"] is True and stats["dropped_events"] == 0


def test_in_memory_proposal_counter_has_no_lock_and_ids_are_distinct():
    async def go():
        hub = await seeded()
        inc = hub.incidents()[0]
        assert not hasattr(hub, "_lock")
        props = await asyncio.gather(*[hub.create_proposal(inc.id, "x") for _ in range(20)])
        assert sorted(int(p.id[1:]) for p in props) == list(range(1, 21))

    asyncio.run(go())


def test_in_memory_proposal_cap_is_unchanged():
    async def go():
        hub = await seeded()
        inc = hub.incidents()[0]
        for _ in range(205):
            await hub.create_proposal(inc.id, "x")
        assert len(hub.list_proposals()) == 200 and hub.list_proposals()[0].id == "p6"

    asyncio.run(go())
