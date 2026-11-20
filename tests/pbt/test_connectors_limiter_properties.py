"""Task 5.2: property 16, rate limit conservation."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.connectors.limiter import DropNotice, TokenBucket

ARRIVALS = st.lists(st.tuples(st.floats(0, 5, allow_nan=False), st.integers(0, 6000)), max_size=60)


@settings(max_examples=500, deadline=None)
@given(ARRIVALS, st.floats(1, 5000), st.floats(1, 5000))
def test_rate_limit_conservation(arrivals, rate, burst):
    bucket = TokenBucket(rate=rate, burst=burst, now=0.0)
    notice = DropNotice(interval=10.0)
    now = 0.0
    admitted = dropped = arrived = reported = 0
    report_times: list[float] = []
    for gap, n in arrivals:
        now += gap
        k = bucket.take(n, now)
        assert 0 <= k <= n
        admitted += k
        dropped += n - k
        arrived += n
        notice.add(n - k)
        assert admitted <= burst + rate * now + 1e-6
        got = notice.due(now)
        if got:
            reported += got
            report_times.append(now)
    assert admitted + dropped == arrived
    assert all(b - a >= 10.0 for a, b in zip(report_times, report_times[1:]))
    final = notice.due(now + 1e9)
    assert reported + final == dropped
