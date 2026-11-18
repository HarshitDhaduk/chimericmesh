"""Event ids, tagged return value, read-only resolved set and the reopen rule on a bare hub."""
import asyncio

import httpx
import pytest

from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub

MSG = "could not write block 1 No space left on device"


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def ev(msg=MSG, host="db01", sev=3, ts=1.0):
    return Event(source="syslog", host=host, app="pg", severity=sev, message=msg, ts=ts)


def make(clock=None):
    hub = Hub(clock=clock or Clock())
    sent: list[str] = []

    async def fake(channel, text):
        sent.append(text)

    hub.irc_broadcast = fake
    return hub, sent


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, url, **kw)

    return asyncio.run(run())


async def open_incident(hub, n=3):
    for _ in range(n):
        await hub.ingest(ev())
    return hub.incidents()[0]


def test_ids_start_at_one_and_strictly_increase():
    async def go():
        hub, _ = make()
        ids = [(await hub.ingest(ev(f"m{n}"))).id for n in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert [e.id for e in hub.events] == ids
        assert [e.id for e in hub.recent(10)] == ids[::-1]

    asyncio.run(go())


def test_api_events_and_ingest_carry_the_id():
    hub, _ = make()
    app = create_app(hub, start_listeners=False)
    r = call(app, "POST", "/api/ingest", json={"source": "syslog", "line": "<27>Nov 13 11:10:08 h pg[1]: boom"})
    assert r.json()["id"] == 1
    assert [e["id"] for e in call(app, "GET", "/api/events").json()] == [1]


def test_storm_output_is_unchanged():
    hub, _ = make()
    app = create_app(hub, start_listeners=False)
    body = call(app, "POST", "/api/demo/storm").json()
    assert set(body) == {"injected", "at"} and body["injected"] == 9


def test_resolved_is_a_read_only_set_of_resolved_ids():
    async def go():
        hub, _ = make()
        assert not hub.resolved and isinstance(hub.resolved, (set, frozenset))
        inc = await open_incident(hub)
        assert not hub.resolved
        await hub.resolve(inc.id)
        assert hub.resolved == {inc.id}
        hub.resolved.discard(inc.id)  # a copy: no effect
        assert hub.resolved == {inc.id} and hub.get_incident(inc.id).resolved
        with pytest.raises(AttributeError):
            hub.resolved = set()  # type: ignore[misc]

    asyncio.run(go())


def test_resolve_records_resolved_at_once():
    async def go():
        clock = Clock()
        hub, _ = make(clock)
        inc = await open_incident(hub)
        await hub.resolve(inc.id)
        first = inc.resolved_at
        assert first == 5000.0
        clock.t += 50
        await hub.resolve(inc.id)
        assert inc.resolved_at == first

    asyncio.run(go())


def lines(sent, prefix):
    return [s for s in sent if s.startswith(prefix)]


def test_inside_the_window_events_add_to_the_count_without_announcing():
    async def go():
        clock = Clock()
        hub, sent = make(clock)
        inc = await open_incident(hub)
        await hub.resolve(inc.id)
        before = len(sent)
        clock.t += 299
        await hub.ingest(ev())
        assert inc.count == 4 and inc.resolved and inc.opening == 1
        assert len(sent) == before

    asyncio.run(go())


def test_after_the_window_the_incident_reopens_with_one_line_and_returns():
    async def go():
        clock = Clock()
        hub, sent = make(clock)
        inc = await open_incident(hub)
        await hub.resolve(inc.id)
        clock.t += 300
        await hub.ingest(ev())
        assert inc.opening == 2 and inc.count == 1 and not inc.resolved and inc.resolved_at is None
        assert lines(sent, "REOPENED") == [f"REOPENED {inc.id}: disk on db01"]
        for _ in range(4):  # count passes 3 and 10 boundaries: no second opening line
            await hub.ingest(ev())
        assert len(lines(sent, "REOPENED")) == 1
        assert sum(1 for s in sent if s.startswith("[") and inc.id in s) == 1  # only the first opening

    asyncio.run(go())


def test_two_concurrent_events_reopen_once():
    async def go():
        clock = Clock()
        hub, sent = make(clock)
        inc = await open_incident(hub)
        await hub.resolve(inc.id)
        clock.t += 300
        await asyncio.gather(hub.ingest(ev()), hub.ingest(ev()))
        assert inc.opening == 2
        assert len(lines(sent, "REOPENED")) == 1

    asyncio.run(go())


def test_info_events_never_reopen():
    async def go():
        clock = Clock()
        hub, sent = make(clock)
        inc = await open_incident(hub)
        await hub.resolve(inc.id)
        clock.t += 1000
        await hub.ingest(ev(sev=6))  # severity above 4 is ignored by the tracker
        assert inc.resolved and inc.opening == 1 and not lines(sent, "REOPENED")

    asyncio.run(go())


def test_reopen_after_env_value(monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_REOPEN_AFTER_SECONDS", "10")
    assert Hub().lifecycle.reopen_after == 10
    monkeypatch.setenv("CHIMERICMESH_REOPEN_AFTER_SECONDS", "junk")
    assert Hub().lifecycle.reopen_after == 300


def test_audit_property_is_settable_after_construction():
    hub = Hub()
    assert hub.audit is None
    sentinel = object()
    hub.audit = sentinel  # type: ignore[assignment]
    assert hub.lifecycle._audit_getter() is sentinel


def test_proposal_names_are_still_importable_from_hub():
    from chimericmesh.hub import MAX_ACTION, MAX_DETAILS, MAX_PROPOSALS, PROPOSAL_STATUSES, Proposal
    from chimericmesh.store.models import Proposal as Model

    assert Proposal is Model and MAX_PROPOSALS == 200 and MAX_ACTION == 200 and MAX_DETAILS == 500
    assert PROPOSAL_STATUSES == ("pending", "approved", "denied", "done")
