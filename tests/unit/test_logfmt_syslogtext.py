"""Task 1.2: the ``plain`` and ``syslog-text`` formats."""
from __future__ import annotations

import dataclasses

from chimericmesh.parsers.logctx import FormatContext, ParseOutcome
from chimericmesh.parsers.plain import parse_plain
from chimericmesh.parsers.syslogtext import parse_syslog_text

CTX = FormatContext(machine="box", service_id="mail", app="maillog")


def test_plain_severity_host_app():
    out = parse_plain("error: x", CTX)
    assert out.matched and out.event is not None
    assert out.event.severity == 3 and out.event.message == "error: x"
    assert out.event.host == "box/mail" and out.event.app == "maillog" and out.event.source == "file"
    assert parse_plain("hello", CTX).event.severity == 6
    assert parse_plain("fatal", CTX).event.severity == 2


def test_plain_blank_line_is_skipped():
    assert parse_plain("", CTX) == ParseOutcome(None, True)
    assert parse_plain("   \t ", CTX) == ParseOutcome(None, True)


def test_syslog_text_rfc3164_without_pri():
    out = parse_syslog_text("Nov 18 10:00:01 mail01 postfix/smtp[123]: connect to x timed out", CTX)
    assert out.matched
    ev = out.event
    assert ev.host == "mail01/mail" and ev.app == "postfix/smtp"
    assert ev.message == "connect to x timed out" and ev.severity == 4 and ev.source == "file"


def test_syslog_text_hints():
    base = "Nov 18 10:00:01 mail01 postfix/smtp[1]: to=<a@b> "
    assert parse_syslog_text(base + "status=deferred", CTX).event.severity == 4
    assert parse_syslog_text(base + "status=bounced", CTX).event.severity == 3
    assert parse_syslog_text(base + "status=rejected", CTX).event.severity == 3
    assert parse_syslog_text(base + "fatal status=deferred", CTX).event.severity == 2


def test_syslog_text_pri_is_kept_and_hints_only_make_it_more_severe():
    line = "<38>Nov 18 10:00:01 mail01 postfix/smtp[1]: ok status=deferred"
    assert parse_syslog_text(line, CTX).event.severity == 4  # PRI 38 = severity 6, hint 4
    line2 = "<27>Nov 18 10:00:01 mail01 postfix/smtp[1]: ok status=deferred"
    assert parse_syslog_text(line2, CTX).event.severity == 3  # PRI severity 3 stays


def test_syslog_text_iso_form():
    out = parse_syslog_text("2026-11-18T10:00:01.123456+00:00 web01 nginx[9]: upstream timed out", CTX)
    assert out.matched
    assert out.event.host == "web01/mail" and out.event.app == "nginx"
    assert out.event.message == "upstream timed out" and out.event.severity == 4


def test_syslog_text_rfc5424_host_dash_is_no_host():
    out = parse_syslog_text("1 2026-11-18T10:00:01Z - app 1 ID47 - something failed", CTX)
    assert out.matched
    assert out.event.host == "box/mail" and out.event.app == "app"
    assert out.event.message == "something failed"


def test_syslog_text_app_named_syslog_is_matched():
    out = parse_syslog_text("Nov 18 10:00:01 h1 syslog: started", CTX)
    assert out.matched and out.event.app == "syslog"


def test_syslog_text_unmatched_line_is_plain():
    out = parse_syslog_text("just some text", CTX)
    assert not out.matched
    expected = parse_plain("just some text", CTX).event
    assert dataclasses.replace(out.event, ts=0) == dataclasses.replace(expected, ts=0)
    assert out.event.host == "box/mail"


def test_syslog_text_without_real_service_uses_bare_line_host():
    ctx = FormatContext(machine="box", service_id=None)
    out = parse_syslog_text("Nov 18 10:00:01 mail01 postfix[1]: hi", ctx)
    assert out.event.host == "mail01"


def test_syslog_text_long_line_host_is_cut():
    line = "Nov 18 10:00:01 " + "h" * 3000 + " app: msg"
    out = parse_syslog_text(line, CTX)
    assert out.matched and len(out.event.host) <= 191


def test_syslog_text_blank_line_is_skipped():
    assert parse_syslog_text("", CTX) == ParseOutcome(None, True)
