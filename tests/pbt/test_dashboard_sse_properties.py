"""Properties 1 to 3 of the dashboard design: frame integrity, bounded payload, total resume parsing."""
import json
import re

from hypothesis import HealthCheck, given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.web import sse

SET = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])

HOSTILE = st.sampled_from(["\n", "\r", "\r\n", "data:", "id:", "event:", "retry:", "\u2028", "\u2029", "\x00",
                           "\ud800", "\udfff", ": hb", "\n\n", " ", "é", "😀"])
TEXT = st.one_of(
    st.text(max_size=300),
    st.lists(st.one_of(HOSTILE, st.text(max_size=10)), max_size=40).map("".join),
    st.integers(4000, 20000).map(lambda n: "data: x\n" * (n // 8)),  # up to 20000 characters
)


def reference_parse(stream: str) -> list[dict]:
    """The WHATWG event-stream parsing rules (lines split on CRLF, LF or CR)."""
    events: list[dict] = []
    data: list[str] = []
    event_type = ""
    last_id = None
    for line in re.split(r"\r\n|\n|\r", stream):
        if line == "":
            if data:
                events.append({"event": event_type or "message", "data": "\n".join(data), "id": last_id})
            data, event_type = [], ""
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name == "event":
            event_type = value
        elif name == "id" and "\x00" not in value:
            last_id = value
    return events


def make_event(message, host, app, severity, eid):
    return Event(source="syslog", host=host, app=app, severity=severity, message=message, raw=message,
                 id=eid, ts=1.0, service_id=None)


EVENTS = st.builds(make_event, TEXT, TEXT, TEXT, st.integers(0, 7), st.integers(0, 2**53))


@SET
@given(EVENTS)
def test_property_1_event_frame_parses_back_to_exactly_one_event(event):
    """Property 1: Frame integrity."""
    frame = sse.frame_event(event.id, sse.event_payload(event))
    parsed = reference_parse(frame)
    assert len(parsed) == 1
    assert parsed[0]["event"] == "message" and parsed[0]["id"] == str(event.id)
    obj = json.loads(parsed[0]["data"])
    assert obj["id"] == event.id and set(obj) == {
        "id", "ts", "source", "host", "app", "severity", "severity_name", "message", "service_id", "log_source_id"}
    assert "\n" not in parsed[0]["data"]


@SET
@given(st.sampled_from(["dropped", "closed"]), TEXT)
def test_property_1_named_frames_and_comments_cannot_forge_events(name, text):
    payload = json.dumps({"reason": text, "count": 1}, ensure_ascii=True)
    parsed = reference_parse(sse.frame_named(name, payload))
    assert len(parsed) == 1 and parsed[0]["event"] == name and json.loads(parsed[0]["data"])["reason"] == text
    assert reference_parse(sse.frame_comment(text)) == []


@SET
@given(st.lists(EVENTS, min_size=1, max_size=6))
def test_property_1_a_run_of_frames_parses_in_order(events):
    stream = ": open\n\n" + "".join(sse.frame_event(e.id, sse.event_payload(e)) for e in events) + ": hb\n\n"
    parsed = reference_parse(stream)
    assert [p["id"] for p in parsed] == [str(e.id) for e in events]


@SET
@given(EVENTS)
def test_property_2_payload_is_one_bounded_ascii_line(event):
    """Property 2: Payload is one bounded line."""
    p = sse.event_payload(event)
    assert p.isascii() and not re.search(r"[\n\r\x00\u2028\u2029]", p)
    assert len(p) <= 60000
    obj = json.loads(p)
    assert len(obj["message"]) <= sse.MAX_MESSAGE_CHARS
    assert len(obj["host"]) <= 255 and len(obj["app"]) <= 255
    assert "raw" not in obj


@SET
@given(st.one_of(st.none(), TEXT, st.from_regex(r"[0-9]{0,20}", fullmatch=True), st.text(alphabet="0123456789\u0663 -+e\n")))
def test_property_3_resume_parsing_is_total(value):
    """Property 3: Resume parsing is total."""
    got = sse.parse_last_event_id(value)
    assert got is None or (isinstance(got, int) and 0 <= got <= 2**53)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value, re.ASCII) and int(value) <= 2**53:
        assert got == int(value)
    else:
        assert got is None
    try:
        after = sse.parse_after(value)
    except ValueError:
        assert value is not None and got is None
    else:
        assert after == (got if value is not None else None)
