import asyncio

import httpx

from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import RATE_WINDOW, Hub

SOURCES = ("syslog", "irc", "telnet")


def ev(src="syslog", sev=6):
    return Event(source=src, host="h1", app="a", severity=sev, message="hello")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def ingest_all(hub, events):
    async def run():
        for e in events:
            await hub.ingest(e)

    asyncio.run(run())


def test_totals_grow_past_buffer_capacity():
    hub = Hub(capacity=5)
    ingest_all(hub, [ev(SOURCES[i % 3]) for i in range(20)])
    s = hub.stats()
    assert s["total_events"] == 20 and s["events"] == 5
    assert s["buffer_capacity"] == 5
    assert s["total_by_source"] == {"syslog": 7, "irc": 7, "telnet": 6}
    assert sum(s["total_by_source"].values()) == s["total_events"]
    assert sum(s["by_source"].values()) == s["events"]


def test_early_return_events_are_counted():
    hub = Hub(capacity=3)
    ingest_all(hub, [ev(sev=7), ev(sev=6), ev(sev=5), ev(sev=4), ev(sev=3)])
    assert hub.stats()["total_events"] == 5


def test_backward_compatible_keys():
    hub = Hub(capacity=2)
    ingest_all(hub, [ev("irc"), ev("irc"), ev("telnet")])
    s = hub.stats()
    assert s["events"] == 2
    assert s["by_source"] == {"irc": 1, "telnet": 1}
    assert {"incidents", "open_incidents", "events_per_second", "uptime_seconds"} <= s.keys()
    assert isinstance(s["uptime_seconds"], int)


def test_rate_idle_burst_and_decay():
    clock = Clock()
    hub = Hub(clock=clock)
    assert hub.rate() == 0.0
    ingest_all(hub, [ev() for _ in range(50)])
    assert hub.rate() == 5.0
    clock.t += RATE_WINDOW + 1
    assert hub.rate() == 0.0
    assert hub.stats()["uptime_seconds"] == RATE_WINDOW + 1


def test_rate_structure_is_bounded():
    clock = Clock()
    hub = Hub(clock=clock)

    async def run():
        for _ in range(100):
            clock.t += 1
            await hub.ingest(ev())

    asyncio.run(run())
    assert len(hub._buckets) <= RATE_WINDOW


def test_api_stats_returns_new_keys():
    async def run():
        hub = Hub(capacity=4)
        app = create_app(hub, start_listeners=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/api/demo/storm")
            await c.post("/api/demo/storm")
            s = (await c.get("/api/stats")).json()
        assert s["total_events"] == 18 and s["events"] == 4 and s["buffer_capacity"] == 4
        assert sum(s["total_by_source"].values()) == 18
        assert {"events_per_second", "uptime_seconds"} <= s.keys()

    asyncio.run(run())
