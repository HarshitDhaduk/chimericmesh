"""SSE framing and resume parsing (dashboard task 2.1; R5.1, R5.2, Success Criterion 3)."""
import json

import pytest

from chimericmesh.events import Event
from chimericmesh.web import sse

KEYS = {"id", "ts", "source", "host", "app", "severity", "severity_name", "message", "service_id", "log_source_id"}


def ev(message="m", host="h", app="a", **kw):
    return Event(source="syslog", host=host, app=app, severity=kw.pop("severity", 3), message=message,
                 raw="RAW-SECRET", id=kw.pop("id", 7), ts=1.5, **kw)


def test_payload_has_exactly_the_ten_keys_and_no_raw():
    p = sse.event_payload(ev(service_id="web", log_source_id="ls1"))
    assert "\n" not in p and "\r" not in p and p.isascii()
    obj = json.loads(p)
    assert set(obj) == KEYS
    assert obj["severity_name"] == "err" and obj["service_id"] == "web" and obj["id"] == 7
    assert "RAW-SECRET" not in p


def test_line_breaks_and_separators_round_trip():
    msg = "a\nb\rc\u2028d\u2029e\x00f"
    p = sse.event_payload(ev(message=msg))
    assert not any(ch in p for ch in "\n\r\u2028\u2029\x00")
    assert json.loads(p)["message"] == msg


def test_long_fields_are_cut():
    obj = json.loads(sse.event_payload(ev(message="x" * 5000, host="h" * 400, app="p" * 400)))
    assert len(obj["message"]) == sse.MAX_MESSAGE_CHARS == 4096
    assert len(obj["host"]) == 255 and len(obj["app"]) == 255


def test_lone_surrogates_and_nul_do_not_raise():
    p = sse.event_payload(ev(message="a\ud800b\x00", host="\udfff", app="\ud800"))
    json.loads(p)
    assert p.isascii()


def test_frame_event_shape():
    p = sse.event_payload(ev())
    assert sse.frame_event(7, p) == "id: 7\ndata: " + p + "\n\n"


def test_forged_fields_stay_inside_one_data_line():
    p = sse.event_payload(ev(message='x\ndata: {"forged":1}\n\n'))
    frame = sse.frame_event(7, p)
    lines = [ln for ln in frame.split("\n") if ln]
    assert len(lines) == 2 and lines[0] == "id: 7" and lines[1].startswith("data: ")
    assert frame.endswith("\n\n") and frame.count("\n\n") == 1


def test_frame_named_accepts_only_the_two_names():
    assert sse.frame_named("dropped", '{"count":3}') == 'event: dropped\ndata: {"count":3}\n\n'
    assert sse.frame_named("closed", '{"reason":"max_age"}').startswith("event: closed\n")
    with pytest.raises(ValueError):
        sse.frame_named("message", "{}")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        sse.frame_named("dropped\nid: 1", "{}")  # type: ignore[arg-type]


def test_frame_comment_has_no_line_break():
    f = sse.frame_comment("a\nb\r\nc")
    assert f.startswith(":") and f.endswith("\n\n") and f.count("\n") == 2 and "\r" not in f
    assert sse.frame_comment() == ": hb\n\n"


@pytest.mark.parametrize("value", [None, "", "-1", "1e3", "abc", "1" * 17, "\u0663\u0664", " 1", "1 ", "1\n", "+1", "9" * 16])
def test_parse_last_event_id_rejects_bad_values(value):
    assert sse.parse_last_event_id(value) is None


@pytest.mark.parametrize("value,expected", [("0", 0), ("42", 42), ("0007", 7), ("9007199254740992", 2**53)])
def test_parse_last_event_id_accepts_digits(value, expected):
    assert sse.parse_last_event_id(value) == expected


def test_parse_after():
    assert sse.parse_after(None) is None
    assert sse.parse_after("0") == 0 and sse.parse_after("42") == 42
    for bad in ["", "-1", "1e3", "abc", "1" * 17, "\u0663", "1\n"]:
        with pytest.raises(ValueError):
            sse.parse_after(bad)
