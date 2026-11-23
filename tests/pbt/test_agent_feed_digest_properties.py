"""Properties 1 and 2 of agent-loop: ``service_events`` limits and order; the digest is deterministic."""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from agent_helpers import FakeState, T0, make_event, make_incident  # noqa: E402
from chimericmesh.agent.digest import Window, build_digest  # noqa: E402
from chimericmesh.agent.feed import service_events  # noqa: E402

SERVICES = ["a", "b", "c"]


@st.composite
def event_lists(draw, max_size=260):
    gaps = draw(st.lists(st.integers(1, 5), max_size=max_size))
    ids = [sum(gaps[: i + 1]) for i in range(len(gaps))]
    return [make_event(i, draw(st.sampled_from(SERVICES)), severity=draw(st.integers(0, 7)),
                       message=draw(st.sampled_from(["x", "y", "z\x00w", "d 1", "d 2"])))
            for i in ids]


any_int = st.integers(min_value=-10**6, max_value=10**6)


@settings(max_examples=80, deadline=None)
@given(event_lists(), st.sampled_from(SERVICES), any_int, any_int, st.one_of(st.none(), any_int))
def test_property_1_service_events_limits_and_order(evs, service, limit, after_id, min_sev):
    state = FakeState(evs)
    out = asyncio.run(service_events(state, service, limit=limit, after_id=after_id, min_severity=min_sev))
    got = out["events"]
    ids = [e["id"] for e in got]
    by_id = {e.id: e for e in evs}
    assert len(got) <= 100
    assert len(got) <= max(limit, 1)
    assert ids == sorted(set(ids))                                   # strictly increasing
    assert all(i > max(after_id, 0) for i in ids)
    assert all(by_id[i].service_id == service for i in ids)          # only the requested service
    if min_sev is not None:
        assert all(e["severity"] <= max(0, min(min_sev, 7)) for e in got)
    assert out["next_after_id"] >= after_id
    second = asyncio.run(service_events(state, service, limit=limit, after_id=out["next_after_id"],
                                        min_severity=min_sev))
    assert not set(ids) & {e["id"] for e in second["events"]}


@settings(max_examples=60, deadline=None)
@given(event_lists(max_size=120), st.sampled_from(SERVICES), st.integers(1, 10), st.integers(0, 7))
def test_property_1_paging_visits_every_matching_event_once(evs, service, limit, min_sev):
    state = FakeState(evs)
    seen, cursor = [], 0
    for _ in range(400):
        out = asyncio.run(service_events(state, service, limit=limit, after_id=cursor, min_severity=min_sev))
        seen += [e["id"] for e in out["events"]]
        if out["next_after_id"] == cursor:
            break
        cursor = out["next_after_id"]
    expect = [e.id for e in evs if e.service_id == service and e.severity <= min_sev]
    assert seen == expect


WINDOW = Window(start=T0, end=T0 + 3600, minutes=60)


@st.composite
def digest_inputs(draw):
    events = [make_event(i + 1, "svc", ts=T0 + draw(st.floats(-600, 4200, allow_nan=False)),
                         severity=draw(st.integers(0, 7)),
                         message=draw(st.sampled_from(["disk 1 full", "disk 22 full", "net down", "u\x00x", "a" * 400,
                                                       "b 2", "c 3", "d 4", "e 5", "f 6", "g 7", "h 8", "i 9",
                                                       "j 10", "k 11", "l 12", "m 13"])))
              for i in range(draw(st.integers(0, 60)))]
    incs = [make_incident(f"i{n}", first_seen=T0 + draw(st.floats(-600, 4200, allow_nan=False)))
            for n in range(draw(st.integers(0, 30)))]
    return events, incs, draw(st.integers(0, 2**31))


@settings(max_examples=80, deadline=None)
@given(digest_inputs())
def test_property_2_digest_is_deterministic_and_consistent(data):
    events, incs, seed = data
    out = build_digest("svc", WINDOW, events, incs, scanned=len(events), truncated=False)
    shuffled, shuffled_i = events[:], incs[:]
    random.Random(seed).shuffle(shuffled)
    random.Random(seed).shuffle(shuffled_i)
    assert build_digest("svc", WINDOW, shuffled, shuffled_i, scanned=len(events), truncated=False) == out
    inside = [e for e in events if WINDOW.start <= e.ts <= WINDOW.end]
    assert out["total_events"] == len(inside)
    assert list(out["severity_counts"]) == [str(n) for n in range(8)]
    assert sum(out["severity_counts"].values()) == out["total_events"]
    assert len(out["slices"]) == 5 and sum(s["events"] for s in out["slices"]) == out["total_events"]
    top = out["top_signatures"]
    assert len(top) <= 10
    assert [t["count"] for t in top] == sorted((t["count"] for t in top), reverse=True)
    assert all(len(t["sample"]) <= 200 for t in top)
    assert len(out["incidents_opened"]) <= 20
    firsts = [o["first_seen"] for o in out["incidents_opened"]]
    assert firsts == sorted(firsts, reverse=True)


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=-10**6, max_value=10**6))
def test_property_2_minutes_are_clamped_to_1_240(minutes):
    window = Window.ending(T0 + 10**5, minutes)
    assert 1 <= window.minutes <= 240
    assert window.end - window.start == 60 * window.minutes
