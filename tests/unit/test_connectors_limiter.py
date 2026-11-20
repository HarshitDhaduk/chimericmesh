"""Task 5.1: the per-source token bucket and the drop notice (explicit clocks only)."""
from __future__ import annotations

from chimericmesh.connectors.limiter import DropNotice, TokenBucket


def test_fresh_bucket_admits_burst_then_nothing():
    b = TokenBucket(rate=2000, burst=2000, now=0.0)
    assert b.take(2000, 0.0) == 2000
    assert b.take(1, 0.0) == 0


def test_refill_after_half_a_second():
    b = TokenBucket(now=0.0)
    b.take(2000, 0.0)
    assert b.take(5000, 0.5) == 1000


def test_take_zero_admits_zero():
    b = TokenBucket(now=0.0)
    assert b.take(0, 0.0) == 0
    assert b.take(2000, 0.0) == 2000


def test_arrival_of_5000_admits_2000():
    b = TokenBucket(now=0.0)
    admitted = b.take(5000, 0.0)
    assert admitted == 2000 and 5000 - admitted == 3000


def test_long_idle_does_not_bank_more_than_burst():
    b = TokenBucket(now=0.0)
    b.take(2000, 0.0)
    assert b.take(100_000, 3600.0) == 2000


def test_decreasing_now_is_no_time_passing():
    b = TokenBucket(now=10.0)
    b.take(2000, 10.0)
    assert b.take(10, 5.0) == 0
    assert b.take(2000, 10.5) == 1000


def test_drop_notice_reports_once_per_interval_and_resets():
    n = DropNotice(interval=10.0)
    assert n.due(0.0) == 0
    n.add(5)
    first = 100.0
    assert n.due(first) == 0          # the timer starts at the first drop
    n.add(3)
    assert n.due(first + 9.9) == 0
    assert n.due(first + 10.0) == 8
    assert n.due(first + 10.0) == 0
    n.add(2)
    assert n.due(first + 15.0) == 0   # 10 s since the last report
    assert n.due(first + 20.0) == 2


def test_drop_notice_with_nothing_dropped_is_zero():
    n = DropNotice()
    assert n.due(0.0) == 0
    assert n.due(1000.0) == 0
    n.add(0)
    assert n.due(2000.0) == 0
