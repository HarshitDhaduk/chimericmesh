"""service_events (agent-loop task 2.1; R1.2, R1.5, R1.7)."""
from __future__ import annotations

import asyncio
import json

from agent_helpers import FakeState, make_event
from chimericmesh.agent.feed import service_events


def run(coro):
    return asyncio.run(coro)


def events(n: int, service: str = "svc", **kw):
    return [make_event(i, service, **kw) for i in range(1, n + 1)]


def call(state, **kw):
    kw.setdefault("limit", 25)
    kw.setdefault("after_id", 0)
    kw.setdefault("min_severity", None)
    return run(service_events(state, kw.pop("service_id", "svc"), **kw))


def test_returns_oldest_first_after_after_id():
    out = call(FakeState(events(10)), after_id=4, limit=3)
    assert [e["id"] for e in out["events"]] == [5, 6, 7]
    assert out["service_id"] == "svc"
    assert out["next_after_id"] == 7
    assert out["truncated"] is False


def test_event_shape():
    out = call(FakeState([make_event(1, message="hello", severity=2, host="h", app="a", source="irc")]))
    assert out["events"] == [{"id": 1, "ts": make_event(1).ts, "source": "irc", "host": "h", "app": "a",
                              "severity": 2, "message": "hello"}]


def test_never_more_than_100_whatever_the_caller_passes():
    out = call(FakeState(events(300)), limit=10_000)
    assert len(out["events"]) == 100
    assert out["next_after_id"] == 100


def test_limit_zero_or_negative_still_returns_one():
    assert len(call(FakeState(events(5)), limit=0)["events"]) == 1
    assert len(call(FakeState(events(5)), limit=-9)["events"]) == 1


def test_default_limit_is_25():
    out = run(service_events(FakeState(events(60)), "svc"))
    assert len(out["events"]) == 25


def test_min_severity_keeps_zero_to_n_and_cursor_is_last_scanned():
    evs = [make_event(i, severity=sev) for i, sev in enumerate([6, 6, 3, 7, 2, 6], start=1)]
    out = call(FakeState(evs), min_severity=3)
    assert [e["id"] for e in out["events"]] == [3, 5]
    assert out["next_after_id"] == 6                      # the last scanned id, not the last kept


def test_second_call_progresses_when_nothing_matched():
    state = FakeState([make_event(i, severity=7) for i in range(1, 11)])
    first = call(state, min_severity=0, limit=5)
    assert first["events"] == [] and first["next_after_id"] == 10
    second = call(state, min_severity=0, after_id=first["next_after_id"])
    assert second["events"] == [] and second["next_after_id"] == 10


def test_nothing_scanned_keeps_after_id():
    assert call(FakeState(events(3)), after_id=3)["next_after_id"] == 3
    assert call(FakeState(), after_id=0)["next_after_id"] == 0
    assert call(FakeState(events(3)), after_id=50)["next_after_id"] == 50


def test_negative_after_id_is_zero():
    out = call(FakeState(events(3)), after_id=-9)
    assert [e["id"] for e in out["events"]] == [1, 2, 3]


def test_scan_stops_after_1000_events():
    state = FakeState([make_event(i, severity=7) for i in range(1, 1501)])
    out = call(state, min_severity=0)
    assert out["events"] == []
    assert out["next_after_id"] == 1000


def test_text_fields_are_cleaned_and_capped():
    nasty = "a\x00b\r\nc\x1b[31m" + "x" * 900
    out = call(FakeState([make_event(1, message=nasty, host="h\nost", app="a\rpp", source="sys\x00log")]))
    ev = out["events"][0]
    assert len(ev["message"]) == 500 and not any(c in ev["message"] for c in "\x00\r\n\x1b")
    assert ev["host"] == "host" and ev["app"] == "app" and ev["source"] == "syslog"


def test_response_over_20_kb_is_cut_with_truncated_and_cursor_of_last_kept():
    evs = [make_event(i, message="m" * 480) for i in range(1, 101)]
    out = call(FakeState(evs), limit=100)
    assert out["truncated"] is True
    kept = out["events"]
    assert 0 < len(kept) < 100
    assert out["next_after_id"] == kept[-1]["id"]
    assert len(json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= 20_480
    nxt = call(FakeState(evs), after_id=out["next_after_id"], limit=100)
    assert nxt["events"][0]["id"] == kept[-1]["id"] + 1


def test_events_of_other_services_never_appear():
    evs = [make_event(i, "a" if i % 2 else "b") for i in range(1, 21)]
    out = call(FakeState(evs), service_id="a")
    assert {e["id"] % 2 for e in out["events"]} == {1}
    assert len(out["events"]) == 10


def test_a_state_that_ignores_the_service_filter_still_leaks_nothing():
    class Leaky(FakeState):
        async def events_after(self, after_id, limit=100, *, source=None, service_id=None):
            return await super().events_after(after_id, limit, source=source, service_id=None)
    evs = [make_event(i, "a" if i % 2 else "b") for i in range(1, 21)]
    out = call(Leaky(evs), service_id="a")
    assert all(e["id"] % 2 == 1 for e in out["events"])


def test_a_bad_min_severity_is_clamped():
    assert len(call(FakeState(events(3)), min_severity=99)["events"]) == 3
    assert call(FakeState(events(3)), min_severity=-5)["events"] == []
