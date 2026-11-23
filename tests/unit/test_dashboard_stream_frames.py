"""The SSE frame generator against a fake feed and a manual clock (dashboard task 2.3; R5.1 to R5.5)."""
import asyncio
import json

import pytest

from chimericmesh.events import Event
from chimericmesh.web.feed import StreamConfig, stream_frames


class FakeFeed:
    def __init__(self):
        self.events: list[Event] = []
        self.calls: list[int] = []  # the after_id of every events_after call
        self.fail = 0  # fail this many next calls
        self.fail_forever = False

    def add(self, service="web", severity=3, n=1, message="m"):
        for _ in range(n):
            i = len(self.events) + 1
            self.events.append(Event(source="syslog", host="h", app="a", severity=severity,
                                     message=f"{message}{i}", id=i, ts=float(i), service_id=service))

    async def events_after(self, after_id, limit=100, *, source=None, service_id=None):
        self.calls.append(after_id)
        if self.fail_forever or self.fail > 0:
            self.fail = max(0, self.fail - 1)
            raise RuntimeError("store down")
        want = None if service_id == "unassigned" else service_id
        out = [e for e in self.events if e.id > after_id and (service_id is None or e.service_id == want)]
        return out[:limit]

    def recent(self, limit=50, source=None):
        return list(reversed(self.events))[:limit]


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(d)
        self.t += d
        await asyncio.sleep(0)


class FakeLease:
    def __init__(self):
        self.calls: list[tuple] = []

    def start(self, now):
        self.calls.append(("start", now))

    def touch(self, now):
        self.calls.append(("touch", now))

    def release(self):
        self.calls.append(("release",))


def gen(feed, clock, *, after=None, tail=0, min_severity=7, service="web", exists=lambda s: True, lease=None, **cfg):
    config = StreamConfig(**{"max_seconds": 3.0, **cfg})
    return stream_frames(feed, service, after=after, tail=tail, min_severity=min_severity, config=config,
                         clock=clock, sleep=clock.sleep, service_exists=exists, lease=lease)


async def drain(g):
    return [f async for f in g]


def run(coro):
    return asyncio.run(coro)


def ids(frames):
    return [int(f.split("\n")[0][4:]) for f in frames if f.startswith("id: ")]


def named(frames, name):
    return [json.loads(f.split("data: ", 1)[1]) for f in frames if f.startswith(f"event: {name}\n")]


def test_first_frame_is_open_and_a_fresh_stream_delivers_only_later_events():
    feed, clock = FakeFeed(), Clock()
    feed.add(n=3)

    async def go():
        frames = []
        async for f in gen(feed, clock, max_seconds=10):
            frames.append(f)
            if len(frames) == 1:
                feed.add(n=2)
        return frames

    frames = run(go())
    assert frames[0] == ": open\n\n"
    assert ids(frames) == [4, 5]


def test_tail_gives_newest_matching_rows_oldest_first_then_live_without_gap():
    feed, clock = FakeFeed(), Clock()
    feed.add(n=150)

    async def go():
        out = []
        async for f in gen(feed, clock, tail=100, max_seconds=10):
            out.append(f)
            if len(out) == 101:
                feed.add(n=2)
        return out

    frames = run(go())
    assert ids(frames) == list(range(51, 153))


def test_tail_with_three_matching_events_and_with_none():
    feed, clock = FakeFeed(), Clock()
    feed.add("web", n=3)
    feed.add("other", n=5)
    assert ids(run(drain(gen(feed, clock, tail=100)))) == [1, 2, 3]
    feed2 = FakeFeed()
    feed2.add("other", n=4)
    frames = run(drain(gen(feed2, Clock(), tail=100)))
    assert ids(frames) == []
    assert feed2.calls and feed2.calls[0] == 4  # starts live after the newest id


def test_tail_for_unassigned_selects_events_without_a_service():
    feed = FakeFeed()
    feed.add(None, n=2)
    feed.add("web", n=2)
    feed.add(None, n=1)
    assert ids(run(drain(gen(feed, Clock(), service="unassigned", tail=10)))) == [1, 2, 5]


def test_tail_with_min_severity_filters_the_backlog():
    feed = FakeFeed()
    feed.add(severity=6)
    feed.add(severity=2)
    feed.add(severity=3)
    feed.add(severity=4)
    assert ids(run(drain(gen(feed, Clock(), tail=10, min_severity=3)))) == [2, 3]


def test_after_ignores_tail_and_delivers_exactly_the_events_above_it_once():
    feed = FakeFeed()
    feed.add(n=6)
    frames = run(drain(gen(feed, Clock(), after=2, tail=100)))
    assert ids(frames) == [3, 4, 5, 6]


def test_other_services_are_never_delivered():
    feed = FakeFeed()
    feed.add("web", n=1)
    feed.add("db", n=1)
    feed.add("web", n=1)
    assert ids(run(drain(gen(feed, Clock(), after=0)))) == [1, 3]


def test_unassigned_selects_service_id_none():
    feed = FakeFeed()
    feed.add(None)
    feed.add("web")
    feed.add(None)
    assert ids(run(drain(gen(feed, Clock(), after=0, service="unassigned")))) == [1, 3]


def test_min_severity_filters_and_the_cursor_passes_filtered_events():
    feed = FakeFeed()
    feed.add(severity=6)
    feed.add(severity=3)
    feed.add(severity=7)
    feed.add(severity=0)
    feed.add(severity=5)
    frames = run(drain(gen(feed, Clock(), after=0, min_severity=3)))
    assert ids(frames) == [2, 4]
    # every cycle after the first asks from the newest id seen, filtered or not
    assert set(feed.calls[1:]) == {5}


def test_overflow_gives_one_dropped_frame_and_the_newest_events():
    feed = FakeFeed()
    feed.add(n=1300)
    frames = run(drain(gen(feed, Clock(), after=0, queue_max=1000, max_seconds=0.3)))
    d = named(frames, "dropped")
    assert d == [{"count": 300}]
    di = next(i for i, f in enumerate(frames) if f.startswith("event: dropped"))
    assert ids(frames[di + 1:]) == list(range(301, 1301))
    assert not any(f.startswith("id:") for f in frames[:di])


def test_page_limit_still_ends_with_the_cursor_at_the_newest_id():
    feed = FakeFeed()
    feed.add(n=1300)
    frames = run(drain(gen(feed, Clock(), after=0, queue_max=5000, page=500, max_pages_per_cycle=2, max_seconds=2)))
    assert ids(frames) == list(range(1, 1301))
    assert feed.calls[-1] == 1300


def test_heartbeat_after_fifteen_seconds_of_silence_and_not_before():
    feed, clock = FakeFeed(), Clock()
    frames = run(drain(gen(feed, clock, after=0, max_seconds=14)))
    assert not any(f.startswith(": hb") for f in frames)
    feed, clock = FakeFeed(), Clock()
    frames = run(drain(gen(feed, clock, after=0, max_seconds=16)))
    assert [f for f in frames if f.startswith(": hb")] == [": hb\n\n"]


def test_max_seconds_gives_closed_max_age_and_returns():
    frames = run(drain(gen(FakeFeed(), Clock(), after=0, max_seconds=1)))
    assert named(frames, "closed") == [{"reason": "max_age"}]
    assert frames[-1].startswith("event: closed")


def test_deleted_service_gives_closed_service_deleted():
    feed, clock = FakeFeed(), Clock()
    state = {"alive": True}

    async def go():
        out = []
        async for f in gen(feed, clock, after=0, max_seconds=100, exists=lambda s: state["alive"]):
            out.append(f)
            state["alive"] = False
        return out

    frames = run(go())
    assert named(frames, "closed") == [{"reason": "service_deleted"}]


def test_three_feed_failures_then_recovery_do_not_close():
    feed, clock = FakeFeed(), Clock()
    feed.add(n=1)
    feed.fail = 3
    frames = run(drain(gen(feed, clock, after=0, max_seconds=10)))
    assert ids(frames) == [1]
    assert named(frames, "closed") == [{"reason": "max_age"}]
    assert any(d > 0 for d in clock.sleeps)


def test_failure_count_resets_after_a_good_call():
    feed, clock = FakeFeed(), Clock()
    feed.add(n=1)
    feed.fail = 4  # below the limit of 5; a second burst would close only if the count had not reset

    async def go():
        out = []
        async for f in gen(feed, clock, after=0, max_seconds=40):
            out.append(f)
            if f.startswith("id: "):
                feed.fail = 4
        return out

    assert named(run(go()), "closed") == [{"reason": "max_age"}]


def test_five_failures_in_a_row_close_with_feed_error():
    feed, clock = FakeFeed(), Clock()
    feed.fail_forever = True
    frames = run(drain(gen(feed, clock, after=0, max_seconds=1000)))
    assert named(frames, "closed") == [{"reason": "feed_error"}]
    assert len(feed.calls) == 5
    assert max(clock.sleeps) <= 5


def test_lease_is_started_touched_each_cycle_and_released_once():
    feed, clock = FakeFeed(), Clock()
    lease = FakeLease()
    run(drain(gen(feed, clock, after=0, max_seconds=1, lease=lease)))
    kinds = [c[0] for c in lease.calls]
    assert kinds[0] == "start" and kinds.count("start") == 1
    assert kinds.count("release") == 1 and kinds[-1] == "release"
    touches = [c[1] for c in lease.calls if c[0] == "touch"]
    assert len(touches) >= 4 and touches == sorted(touches)


def test_cancelling_runs_finally_and_cancelled_error_is_not_swallowed():
    feed = FakeFeed()
    lease = FakeLease()
    clock = Clock()

    async def go():
        seen = []

        async def consume():
            async for f in gen(feed, clock, after=0, max_seconds=10**6, lease=lease):
                seen.append(f)

        task = asyncio.create_task(consume())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return seen

    seen = run(go())
    assert seen and seen[0] == ": open\n\n"
    assert [c for c in lease.calls if c[0] == "release"] == [("release",)]


def test_a_feed_that_raises_cancelled_error_propagates():
    class Cancelling(FakeFeed):
        async def events_after(self, *a, **k):
            raise asyncio.CancelledError

    async def go():
        async for _ in gen(Cancelling(), Clock(), after=0):
            pass

    with pytest.raises(asyncio.CancelledError):
        run(go())
