import asyncio

from hypothesis import given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.hub import Hub


@settings(max_examples=50, deadline=None)
@given(
    seq=st.lists(st.tuples(st.sampled_from(["syslog", "irc", "telnet"]), st.integers(0, 7)), max_size=60),
    capacity=st.integers(1, 50),
)
def test_totals_independent_of_capacity(seq, capacity):
    hub = Hub(capacity=capacity)

    async def run():
        for src, sev in seq:
            await hub.ingest(Event(source=src, host="h", app="a", severity=sev, message="m"))

    asyncio.run(run())
    s = hub.stats()
    assert s["total_events"] == len(seq)
    assert sum(s["total_by_source"].values()) == s["total_events"]
    assert s["events"] == min(len(seq), capacity)
