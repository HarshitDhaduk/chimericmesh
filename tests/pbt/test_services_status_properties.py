"""Properties 10 and 11: the state and error-rate formulas against a naive model."""
from __future__ import annotations

from hypothesis import given, strategies as st

from chimericmesh.services.status import ERROR_WINDOW, StatsTracker, compute_state, error_rate

seconds = st.integers(min_value=0, max_value=2000)


# Feature: services, Property 10: State formula
@given(last_seen=st.one_of(st.none(), st.floats(min_value=0, max_value=5000, allow_nan=False)),
       now=st.floats(min_value=0, max_value=5000, allow_nan=False),
       boundary=st.sampled_from([299, 300, 301]))
def test_state_formula(last_seen, now, boundary):
    expected = "never_seen" if last_seen is None else ("active" if now - last_seen <= 300 else "idle")
    assert compute_state(last_seen, now) == expected
    assert compute_state(1000.0 - boundary, 1000.0) == ("active" if boundary <= 300 else "idle")


# Feature: services, Property 11: Error rate formula
@given(stream=st.lists(st.tuples(seconds, st.integers(min_value=0, max_value=7)), max_size=400),
       lead=st.integers(min_value=0, max_value=600))
def test_error_rate_matches_naive_model(stream, lead):
    tracker = StatsTracker()
    for sec, sev in stream:           # equal and out-of-order seconds allowed
        tracker.observe("svc", sev, float(sec))
    now = float((max((s for s, _ in stream), default=0)) + lead)
    expected = sum(1 for sec, sev in stream if sev <= 3 and now - sec < ERROR_WINDOW)
    snap = tracker.snapshot("svc", now)
    assert snap.errors_in_window == expected
    assert error_rate(snap.errors_in_window) == round(expected / 5, 1)
    assert snap.events_total == len(stream)


@given(errors=st.integers(min_value=0, max_value=10_000))
def test_error_rate_is_errors_over_five_one_decimal(errors):
    assert error_rate(errors) == round(errors / 5, 1)
