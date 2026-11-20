"""Task 1.7: totality, redaction, host and app naming of the file formats (properties 1, 6, 7)."""
from __future__ import annotations

import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.diagnostics import signature
from chimericmesh.parsers.formats import FORMAT_NAMES, get_format
from chimericmesh.parsers.logctx import FormatContext, redact

SETTINGS = settings(max_examples=10_000, deadline=None, suppress_health_check=list(HealthCheck))
MID = settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))

ANY_CHAR = st.characters(exclude_categories=[])
ANY_TEXT = st.text(alphabet=ANY_CHAR, max_size=300)
# Text built from the characters the patterns care about, so the matching branches are reached.
SYNTAX_TEXT = st.text(alphabet='"[] =?#-<>:/0123456789ABCGENTPSUHVa.+\n\r\x00', max_size=200)
TEMPLATES = st.sampled_from([
    '203.0.113.7 - frank [10/Oct/2026:13:55:36 -0700] "GET /a?b=1 HTTP/1.1" 500 12 "http://r/?q=1" "UA"',
    "Nov 18 10:00:01 mail01 postfix/smtp[123]: status=deferred",
    "2026-11-18T10:00:01.123+00:00 web01 nginx[9]: upstream timed out",
    "1 2026-11-18T10:00:01Z - app 1 ID47 - something failed",
    "#Fields: date time cs-method cs-uri-stem cs-uri-query sc-status sc-win32-status",
    "2026-11-18 10:00:00 GET /x q=1 500 64",
    "Thu Nov 18 09:15:17 2026 1 192.0.2.5 1024 /pub/a b.bin b _ o r alice ftp 0 * c",
])
LINES = st.one_of(ANY_TEXT, SYNTAX_TEXT, st.builds(lambda t, a, b: a + t + b, TEMPLATES, ANY_TEXT, SYNTAX_TEXT))
NAMES = st.text(alphabet=ANY_CHAR, max_size=300)
CONTEXTS = st.builds(
    FormatContext,
    machine=NAMES,
    service_id=st.one_of(st.none(), st.just("unassigned"), NAMES),
    source_host=st.one_of(st.none(), NAMES),
    app=NAMES,
    w3c_fields=st.one_of(
        st.none(),
        st.lists(st.sampled_from(["cs-method", "cs-uri-stem", "cs-uri-query", "sc-status", "sc-win32-status", "x"]),
                 max_size=8).map(tuple),
    ),
)


def run(fmt_name: str, line: str, ctx: FormatContext):
    fmt = get_format(fmt_name)
    ctx = fmt.update(line, ctx)
    return fmt.parse_line(line, ctx)


def check_event(ev) -> None:
    assert 0 <= ev.severity <= 7
    assert len(ev.message) <= 4096 and len(ev.raw) <= 1024
    assert ev.source == "file"
    assert ev.host and ev.app


def _total(fmt_name: str, line: str, ctx: FormatContext) -> None:
    out = run(fmt_name, line, ctx)
    if out.event is not None:
        check_event(out.event)


# ---- Property 1: parsers are total ------------------------------------------------------------

@SETTINGS
@given(LINES, CONTEXTS)
def test_property_1_syslog_text_total(line, ctx):
    _total("syslog-text", line, ctx)


@SETTINGS
@given(LINES, CONTEXTS)
def test_property_1_ncsa_total(line, ctx):
    _total("ncsa", line, ctx)


@SETTINGS
@given(LINES, CONTEXTS)
def test_property_1_w3c_total(line, ctx):
    _total("w3c", line, ctx)


@SETTINGS
@given(LINES, CONTEXTS)
def test_property_1_xferlog_total(line, ctx):
    _total("xferlog", line, ctx)


@SETTINGS
@given(LINES, CONTEXTS)
def test_property_1_plain_total(line, ctx):
    _total("plain", line, ctx)


def test_property_1_one_megabyte_lines():
    ctx = FormatContext(w3c_fields=("sc-status",))
    for line in ("= " * 524288, "a" * 1_048_576, "\x00\r\n" * 350_000, '"' * 1_000_000, "[" * 1_000_000,
                 "password=" + "x" * 1_000_000, "1 " * 500_000):
        for name in FORMAT_NAMES:
            _total(name, line, ctx)


# ---- Property 6: redaction --------------------------------------------------------------------

SECRET = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~+/-", min_size=4,
                 max_size=40).map(lambda s: "Zq9" + s)
SCHEME = st.sampled_from(["Bearer", "Basic", "Token", "Digest"])
CTX = FormatContext(machine="box", service_id="svc", app="app")
W3C_CTX = get_format("w3c").update(
    "#Fields: cs-method cs-uri-stem cs-uri-query cs-username sc-status", CTX)


@MID
@given(SECRET, SCHEME)
def test_property_6_redaction_in_every_format(v, scheme):
    cases = {
        "plain": f"login failed password={v} then token={v} and Authorization: {scheme} {v}",
        "syslog-text": f"Nov 18 10:00:01 h app[1]: error password={v} Authorization: {scheme} {v} token={v}",
        "ncsa": (f'203.0.113.7 - - [10/Oct/2026:13:55:36 +0000] "GET /p?token={v}&password={v} HTTP/1.1" 500 1 '
                 f'"http://h/r?password={v}" "Authorization: {scheme} {v}"'),
        "w3c": f"GET /p/token={v} token={v}&password={v} Authorization:+{scheme}+{v} 500",
        "xferlog": f"Thu Nov 18 09:15:17 2026 1 192.0.2.5 1024 /pub/token={v} b _ o r alice ftp 0 * c",
    }
    for name, line in cases.items():
        ctx = W3C_CTX if name == "w3c" else CTX
        out = run(name, line, ctx)
        assert out.event is not None, name
        assert v not in out.event.message, name
        assert v not in out.event.raw, name
        check_event(out.event)


@MID
@given(ANY_TEXT, SECRET)
def test_property_6_redact_is_idempotent_and_bounded(text, v):
    once = redact(text + f" password={v}")
    assert redact(once) == once
    assert v not in once


# ---- Property 7: host and app naming ----------------------------------------------------------

SAFE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=100)
IPS = st.sampled_from(["203.0.113.7", "198.51.100.1", "192.0.2.44", "2001:db8::1", "10.1.2.3"])
HOSTS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=300)


def expected_host(ctx: FormatContext) -> str:
    if ctx.source_host:
        return ctx.source_host
    if ctx.service_id and ctx.service_id != "unassigned":
        return ctx.machine + "/" + ctx.service_id
    return ctx.machine


@MID
@given(SAFE, st.one_of(st.none(), st.just("unassigned"), st.text(alphabet="abcdefghij-0123456789", min_size=1,
                                                              max_size=40)),
       st.one_of(st.none(), SAFE), st.sampled_from(["ncsa", "w3c", "xferlog", "plain"]), IPS,
       st.text(alphabet="abcdefghij", min_size=1, max_size=20))
def test_property_7_host_naming_non_syslog_formats(machine, sid, source_host, fmt, ip, app):
    ctx = FormatContext(machine=machine, service_id=sid, source_host=source_host, app=app)
    lines = {
        "ncsa": f'{ip} - - [10/Oct/2026:13:55:36 +0000] "GET /x HTTP/1.1" 500 1',
        "plain": f"{ip} failed",
        "xferlog": f"Thu Nov 18 09:15:17 2026 1 {ip} 1024 /f b _ o r u ftp 0 * c",
        "w3c": "GET /x 500",
    }
    if fmt == "w3c":
        ctx = get_format("w3c").update("#Fields: cs-method cs-uri-stem sc-status", ctx)
    ev = run(fmt, lines[fmt], ctx).event
    assert ev.host == expected_host(ctx)
    assert ev.app == app
    assert len(ev.host) <= 191
    assert ip not in ev.host


@MID
@given(HOSTS, SAFE, st.one_of(st.none(), st.just("unassigned"), st.text(alphabet="abcdefghij-0123456789",
                                                                       min_size=1, max_size=40)))
def test_property_7_syslog_text_line_host(line_host, machine, sid):
    ctx = FormatContext(machine=machine, service_id=sid, app="fallback")
    ev = run("syslog-text", f"Nov 18 10:00:01 {line_host} app[1]: hello", ctx).event
    suffix = "/" + sid if sid and sid != "unassigned" else ""
    assert ev.host == line_host[:150] + suffix
    assert len(ev.host) <= 191 and ev.app == "app"


@MID
@given(IPS, IPS, SAFE, st.one_of(st.none(), st.text(alphabet="abcdefghij", min_size=1, max_size=20)))
def test_property_7_ncsa_clients_do_not_split_incidents(ip1, ip2, machine, sid):
    ctx = FormatContext(machine=machine, service_id=sid)
    def ev(ip):
        return run("ncsa", f'{ip} - - [10/Oct/2026:13:55:36 +0000] "GET /orders/42 HTTP/1.1" 500 1', ctx).event
    a, b = ev(ip1), ev(ip2)
    assert a.host == b.host and signature(a.message) == signature(b.message)
    assert re.search(re.escape(ip1), a.raw)
