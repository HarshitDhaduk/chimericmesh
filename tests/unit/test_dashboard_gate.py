"""Stream gate and configuration (dashboard task 2.2; R5.3, H4, L5)."""
import dataclasses

import pytest

from chimericmesh.web.feed import MAX_STREAMS, StreamConfig, StreamGate


def test_twenty_acquires_succeed_and_the_next_is_refused():
    g = StreamGate()
    leases = [g.acquire(0.0) for _ in range(20)]
    assert all(leases) and g.active == 20 == MAX_STREAMS
    assert g.acquire(0.0) is None
    assert g.active == 20


def test_release_frees_exactly_one_slot_once():
    g = StreamGate()
    a, b = g.acquire(0.0), g.acquire(0.0)
    a.release()
    assert g.active == 1
    a.release()
    assert g.active == 1
    b.release()
    b.release()
    assert g.active == 0


def test_active_never_goes_below_zero():
    g = StreamGate(max_streams=2)
    lease = g.acquire(0.0)
    for _ in range(5):
        lease.release()
    assert g.active == 0


def test_an_unstarted_lease_is_reclaimed_after_the_grace_period_only():
    g = StreamGate(max_streams=1, lease_grace=10.0)
    assert g.acquire(0.0) is not None
    assert g.acquire(9.5) is None  # not before the grace period has passed
    assert g.acquire(10.5) is not None  # reclaimed
    assert g.active == 1


def test_a_started_lease_is_reclaimed_only_after_stall_after_without_touch():
    g = StreamGate(max_streams=1, lease_grace=10.0, stall_after=10.0)
    lease = g.acquire(0.0)
    lease.start(1.0)
    lease.touch(8.0)
    assert g.acquire(17.0) is None  # touched 9 s ago, within stall_after
    assert g.acquire(18.5) is not None  # last touch 10.5 s ago
    assert g.active == 1


def test_a_started_lease_is_not_reclaimed_by_the_grace_rule():
    g = StreamGate(max_streams=1, lease_grace=10.0, stall_after=100.0)
    lease = g.acquire(0.0)
    lease.start(0.5)
    assert g.acquire(50.0) is None
    lease.touch(60.0)
    assert g.acquire(150.0) is None
    assert g.acquire(161.0) is not None


def test_a_reclaimed_lease_frees_nothing_a_second_time():
    g = StreamGate(max_streams=2, lease_grace=10.0)
    stale = g.acquire(0.0)
    fresh = g.acquire(20.0)  # reclaims `stale`
    assert fresh is not None and g.active == 1
    stale.release()  # late finally of the reclaimed lease
    assert g.active == 1
    stale.touch(30.0)  # also harmless
    stale.start(30.0)
    assert g.active == 1
    fresh.release()
    assert g.active == 0


def test_active_stays_between_zero_and_the_maximum():
    g = StreamGate(max_streams=3, lease_grace=5.0, stall_after=5.0)
    held = []
    for t in range(0, 60, 2):
        lease = g.acquire(float(t))
        if lease is not None:
            if t % 4 == 0:
                lease.start(float(t))
            held.append(lease)
        if held and t % 6 == 0:
            held.pop(0).release()
        assert 0 <= g.active <= 3


def test_stream_config_defaults_are_the_designs_and_frozen():
    c = StreamConfig()
    assert (c.poll_interval, c.heartbeat, c.queue_max, c.page) == (0.25, 15.0, 1000, 500)
    assert (c.max_pages_per_cycle, c.max_seconds, c.max_feed_failures) == (20, 3600.0, 5)
    assert "lease_grace" not in {f.name for f in dataclasses.fields(c)}
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.page = 1  # type: ignore[misc]
