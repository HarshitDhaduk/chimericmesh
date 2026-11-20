"""Task 1.1: the shared helpers of the file formats."""
from __future__ import annotations

import dataclasses
import time
import typing

import pytest

from chimericmesh.events import Event, Source
from chimericmesh.parsers.logctx import (
    FormatContext,
    clean_line,
    file_host,
    http_severity,
    make_file_event,
    redact,
    syslog_text_host,
    truncate,
)


def test_clean_line_removes_nul_controls_cr_lf_but_keeps_tab():
    assert clean_line("a\x00b\x01c\x7fd\re\nf\tg\x1bh") == "abcdef\tgh"


def test_truncate_cuts_to_the_limit():
    assert truncate("x" * 5000, 4096) == "x" * 4096
    assert truncate("abc", 10) == "abc"
    assert len(truncate("x" * 5000)) == 4096


@pytest.mark.parametrize(
    "text,expected",
    [
        ("password=hunter2", "password=<redacted>"),
        ('token="a b"', "token=<redacted>"),
        ("api_key: abc123", "api_key=<redacted>"),
        ("Authorization: Bearer abc.def", "Authorization: <redacted>"),
        ("Authorization: abc", "Authorization: <redacted>"),
        ("a=1&password=zzzz9&b=2", "a=1&password=<redacted>&b=2"),
    ],
)
def test_redact_replaces_the_value(text, expected):
    out = redact(text)
    assert out == expected
    assert redact(out) == out


def test_redact_leaves_ordinary_text_alone():
    text = "GET /index.html -> 200 passing the salt to the user"
    assert redact(text) == text


def test_redact_is_fast_on_hostile_input():
    text = "= " * 524288
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize(
    "status,expected",
    [(200, 6), (301, 6), (404, 5), (408, 4), (429, 4), (499, 5), (500, 3), (503, 3), (599, 3), (600, 6)],
)
def test_http_severity(status, expected):
    assert http_severity(status) == expected


def test_file_host_rules():
    assert file_host(FormatContext(machine="m1", service_id="svc", source_host="web01")) == "web01"
    assert file_host(FormatContext(machine="m1", service_id="svc")) == "m1/svc"
    assert file_host(FormatContext(machine="m1", service_id=None)) == "m1"
    assert file_host(FormatContext(machine="m1", service_id="unassigned")) == "m1"


def test_syslog_text_host_rules():
    ctx = FormatContext(machine="m1", service_id="mail")
    assert syslog_text_host("mail01", ctx) == "mail01/mail"
    assert syslog_text_host("mail01", FormatContext(machine="m1", service_id=None)) == "mail01"
    assert syslog_text_host("mail01", FormatContext(machine="m1", service_id="unassigned")) == "mail01"
    assert syslog_text_host("", ctx) == file_host(ctx)
    assert len(syslog_text_host("h" * 500, FormatContext(service_id="s" * 40))) <= 191


def test_make_file_event_basics():
    ctx = FormatContext(machine="m1", service_id="svc", app="web")
    before = time.time()
    ev = make_file_event(ctx, severity=3, message="boom\x00\r\n token=abc123", raw="r")
    assert ev.source == "file"
    assert ev.host == "m1/svc" and ev.app == "web"
    assert ev.message == "boom token=<redacted>"
    assert before - 1 <= ev.ts <= time.time() + 1  # the Event default: ingest time (O12)


def test_make_file_event_clamps_and_bounds():
    ctx = FormatContext()
    assert make_file_event(ctx, severity=99, message="x", raw="").severity == 7
    assert make_file_event(ctx, severity=-5, message="x", raw="").severity == 0
    ev = make_file_event(ctx, severity=6, message="m" * 9000, raw="r" * 9000)
    assert len(ev.message) == 4096 and len(ev.raw) == 1024


def test_make_file_event_never_returns_empty_host_or_app():
    ctx = FormatContext(machine="", service_id=None, source_host=None, app="")
    ev = make_file_event(ctx, severity=6, message="x", raw="")
    assert ev.host and ev.app
    ev2 = make_file_event(FormatContext(), severity=6, message="x", raw="", host="", app="")
    assert ev2.host and ev2.app


def test_format_context_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FormatContext().machine = "x"  # type: ignore[misc]


def test_source_accepts_file():
    assert "file" in typing.get_args(Source)
    assert isinstance(Event(source="file", host="h", app="a", severity=6, message="m"), Event)
