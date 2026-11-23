"""The digest (agent-loop task 2.2; R1.3, R1.5, R1.7, R1-M7)."""
from __future__ import annotations

import asyncio
import random

from agent_helpers import FakeState, RangeState, T0, make_event, make_incident
from chimericmesh import diagnostics
from chimericmesh.agent.digest import Window, build_digest, collect_window


def run(coro):
    return asyncio.run(coro)


W = Window(start=T0, end=T0 + 3600, minutes=60)      # 60 minutes, 12-minute slices


def digest(events, incidents=(), *, window=W, scanned=None, truncated=False, service="svc"):
    return build_digest(service, window, list(events), list(incidents),
                        scanned=len(events) if scanned is None else scanned, truncated=truncated)


def ev(i, ts, message="boom", severity=3, **kw):
    return make_event(i, "svc", ts=ts, message=message, severity=severity, **kw)


def test_window_ending_builds_start_from_minutes():
    w = Window.ending(T0 + 1000, 5)
    assert (w.start, w.end, w.minutes) == (T0 + 700, T0 + 1000, 5)


def test_severity_counts_have_all_eight_keys_and_sum_to_total():
    out = digest([ev(1, T0 + 10, severity=3), ev(2, T0 + 20, severity=3), ev(3, T0 + 30, severity=0)])
    assert list(out["severity_counts"]) == [str(n) for n in range(8)]
    assert out["severity_counts"]["3"] == 2 and out["severity_counts"]["0"] == 1
    assert sum(out["severity_counts"].values()) == out["total_events"] == 3


def test_top_signatures_use_diagnostics_signature_and_order():
    events = [ev(1, T0 + 5, "Disk 11 full"), ev(2, T0 + 6, "Disk 22 full"), ev(3, T0 + 7, "Net down"),
              ev(4, T0 + 8, "Net down"), ev(5, T0 + 9, "Aaa up")]
    top = digest(events)["top_signatures"]
    assert [t["signature"] for t in top] == [diagnostics.signature("Disk 1 full"), "net down", "aaa up"]
    assert [t["count"] for t in top] == [2, 2, 1]               # tie on count: earlier first_ts first
    assert top[0]["first_ts"] == T0 + 5 and top[0]["last_ts"] == T0 + 6
    assert top[0]["sample"] == "Disk 11 full"                   # the message of the earliest event


def test_ties_break_on_the_signature_text():
    top = digest([ev(1, T0 + 5, "zeta"), ev(2, T0 + 5, "alpha")])["top_signatures"]
    assert [t["signature"] for t in top] == ["alpha", "zeta"]


def test_at_most_ten_signatures_and_sample_at_most_200_chars():
    events = [ev(i, T0 + i, f"unique{chr(97 + i)}x" + "y" * 400) for i in range(15)]
    top = digest(events)["top_signatures"]
    assert len(top) == 10
    assert all(len(t["sample"]) <= 200 and len(t["signature"]) <= 200 for t in top)


def test_most_severe_is_the_lowest_severity_of_the_signature():
    top = digest([ev(1, T0 + 1, "same", severity=5), ev(2, T0 + 2, "same", severity=2)])["top_signatures"]
    assert top[0]["most_severe"] == 2


def test_five_equal_slices_with_per_minute():
    events = [ev(1, T0 + 1), ev(2, T0 + 719), ev(3, T0 + 720), ev(4, T0 + 3599), ev(5, T0 + 3600)]
    out = digest(events)
    slices = out["slices"]
    assert len(slices) == 5
    assert [s["events"] for s in slices] == [2, 1, 0, 0, 2]     # ts == end lands in the last slice
    assert slices[0]["start"] == T0 and slices[0]["end"] == T0 + 720
    assert slices[4]["end"] == T0 + 3600
    assert slices[0]["per_minute"] == round(2 / 12, 2)
    assert sum(s["events"] for s in slices) == out["total_events"] == 5


def test_events_outside_the_window_are_not_counted():
    out = digest([ev(1, T0 - 1), ev(2, T0), ev(3, T0 + 3600), ev(4, T0 + 3601)])
    assert out["total_events"] == 2


def test_a_future_dated_event_is_never_counted():
    out = digest([ev(1, T0 + 10), ev(2, T0 + 10**9)])
    assert out["total_events"] == 1 and sum(out["severity_counts"].values()) == 1


def test_incidents_opened_in_the_window_newest_first_at_most_20():
    incs = [make_incident(f"i{n}", first_seen=T0 + n * 10, severity=2, count=4, opening=2) for n in range(1, 26)]
    incs.append(make_incident("old", first_seen=T0 - 5))
    incs.append(make_incident("late", first_seen=T0 + 4000))
    opened = digest([], incs)["incidents_opened"]
    assert len(opened) == 20
    assert [o["id"] for o in opened][:3] == ["i25", "i24", "i23"]
    assert set(opened[0]) == {"id", "category", "severity", "count", "opening", "first_seen"}
    assert "old" not in {o["id"] for o in opened} and "late" not in {o["id"] for o in opened}


def test_same_events_in_another_order_give_an_identical_result():
    rng = random.Random(7)
    events = [ev(i, T0 + rng.randrange(0, 3600), rng.choice(["a 1", "b 2", "c 3", "a 9"]),
                 severity=rng.randrange(8)) for i in range(1, 60)]
    incs = [make_incident(f"i{n}", first_seen=T0 + rng.randrange(0, 3600)) for n in range(8)]
    base = digest(events, incs)
    for seed in range(5):
        shuffled, shuffled_i = events[:], incs[:]
        random.Random(seed).shuffle(shuffled)
        random.Random(seed).shuffle(shuffled_i)
        assert digest(shuffled, shuffled_i) == base


def test_empty_digest_is_zeros_not_an_error():
    out = digest([])
    assert out["total_events"] == 0 and out["top_signatures"] == [] and out["incidents_opened"] == []
    assert [s["events"] for s in out["slices"]] == [0] * 5
    assert out["scanned"] == 0 and out["truncated"] is False
    assert out["service_id"] == "svc" and out["minutes"] == 60
    assert out["window"] == {"start": T0, "end": T0 + 3600}


def test_text_is_cleaned():
    out = digest([ev(1, T0 + 1, "a\x00b\r\nc\x1b" + "z" * 600)])
    sample = out["top_signatures"][0]["sample"]
    assert not any(c in sample for c in "\x00\r\n\x1b")


def test_scanned_and_truncated_are_passed_through():
    out = digest([ev(1, T0 + 1)], scanned=77, truncated=True)
    assert out["scanned"] == 77 and out["truncated"] is True


# ---- collect_window -------------------------------------------------------------------------------

def test_collect_window_binary_search_reads_at_most_20_single_events():
    state = RangeState(100_000)                                  # ts = T0 + id, one per second
    window = Window.ending(state.now(), 10)                      # about 600 events
    events, truncated = run(collect_window(state, "svc", window))
    singles = [c for c in state.after_calls if c[1] == 1]
    assert len(singles) <= 20
    in_window = [e for e in events if window.start <= e.ts <= window.end]
    assert len(in_window) == 601 and not truncated
    assert len(events) < 800                                     # it did not read from id 0
    assert [e.id for e in events] == sorted(e.id for e in events)


def test_collect_window_pages_the_services_events_by_100():
    state = FakeState([make_event(i, "svc", ts=T0 + i) for i in range(1, 251)], now=T0 + 250)
    events, truncated = run(collect_window(state, "svc", Window.ending(T0 + 250, 60)))
    assert [e.id for e in events] == list(range(1, 251)) and not truncated
    assert all(c[2] == "svc" for c in state.after_calls)
    assert max(c[1] for c in state.after_calls) == 100


def test_collect_window_sets_truncated_at_the_scan_cap():
    state = RangeState(30_000, step=0.001)                       # everything lies inside the window
    events, truncated = run(collect_window(state, "svc", Window.ending(state.now(), 60)))
    assert truncated is True and len(events) == 20_000


def test_collect_window_exactly_at_the_cap_is_not_truncated():
    state = RangeState(20_000, step=0.001)
    events, truncated = run(collect_window(state, "svc", Window.ending(state.now(), 60)))
    assert len(events) == 20_000 and truncated is False


def test_collect_window_empty_service_gives_nothing():
    events, truncated = run(collect_window(FakeState(), "svc", Window.ending(T0, 5)))
    assert events == [] and truncated is False
    other = FakeState([make_event(1, "other", ts=T0)], now=T0)
    assert run(collect_window(other, "svc", Window.ending(T0 + 10, 5))) == ([], False)


def test_one_far_future_event_is_skipped_and_later_events_still_count():
    n, end = 1000, T0 + 1000
    state = RangeState(n, ts_of=lambda i: T0 + 10**9 if i == 500 else T0 + i, now=end)
    window = Window.ending(end, 60)
    events, truncated = run(collect_window(state, "svc", window))
    assert truncated is False
    counted = digest(events, window=window, scanned=len(events))["total_events"]
    assert counted == n - 1                                      # only the future event is dropped


def test_200_consecutive_future_events_stop_the_scan_as_truncated():
    end = T0 + 1000
    state = RangeState(2000, ts_of=lambda i: T0 + 10**9 if 100 <= i < 300 else T0 + (i % 900), now=end)
    events, truncated = run(collect_window(state, "svc", Window.ending(end, 60)))
    assert truncated is True
    assert max(e.id for e in events) == 299                      # stopped right after the 200th
    assert len(state.after_calls) < 40


def test_199_consecutive_future_events_do_not_stop_the_scan():
    end = T0 + 1000
    state = RangeState(600, ts_of=lambda i: T0 + 10**9 if 100 <= i < 299 else T0 + (i % 900), now=end)
    events, truncated = run(collect_window(state, "svc", Window.ending(end, 60)))
    assert truncated is False and max(e.id for e in events) == 600
