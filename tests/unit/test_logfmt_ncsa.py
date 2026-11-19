"""Task 1.3: the ``ncsa`` format (Common and Combined Log Format)."""
from __future__ import annotations

import dataclasses

import pytest

from chimericmesh.diagnostics import signature
from chimericmesh.parsers.logctx import FormatContext
from chimericmesh.parsers.ncsa import parse_ncsa
from chimericmesh.parsers.plain import parse_plain

CTX = FormatContext(machine="box", service_id="web", app="access")


def line(request: str = "GET /orders/42 HTTP/1.1", status: int | str = 200, client: str = "203.0.113.7",
         combined: str = "") -> str:
    base = f'{client} - frank [10/Oct/2026:13:55:36 -0700] "{request}" {status} 2326'
    return base + combined


def test_common_line_parses():
    out = parse_ncsa(line(), CTX)
    assert out.matched and out.event.message == "GET /orders/42 -> 200"
    assert out.event.severity == 6 and out.event.app == "access" and out.event.source == "file"


def test_combined_line_parses():
    text = line(combined=' "http://example.com/start.html" "Mozilla/4.08 [en] (Win98; I ;Nav)"')
    out = parse_ncsa(text, CTX)
    assert out.matched and out.event.message == "GET /orders/42 -> 200"
    assert "Mozilla/4.08" in out.event.raw


def test_query_string_never_reaches_message_or_raw():
    out = parse_ncsa(line("GET /orders/42?token=abc HTTP/1.1", 500), CTX)
    assert out.event.message == "GET /orders/42 -> 500" and out.event.severity == 3
    for text in (out.event.message, out.event.raw):
        assert "?" not in text and "abc" not in text


@pytest.mark.parametrize("status,sev", [(200, 6), (301, 6), (404, 5), (408, 4), (429, 4), (503, 3)])
def test_status_mapping(status, sev):
    assert parse_ncsa(line(status=status), CTX).event.severity == sev


@pytest.mark.parametrize("request_text", ["-", "\x16\x03\x01", "A" * 5000])
def test_non_standard_request_line(request_text):
    out = parse_ncsa(line(request_text, 400), CTX)
    assert out.matched
    expected_head = request_text.replace("\x16", "").replace("\x03", "").replace("\x01", "")[:100]
    assert out.event.message == f'"{expected_head}" -> 400'


def test_client_ip_stays_in_raw_only():
    a = parse_ncsa(line(client="203.0.113.7"), CTX).event
    b = parse_ncsa(line(client="198.51.100.1"), CTX).event
    assert a.host == b.host == "box/web"
    assert "203.0.113.7" in a.raw and "203.0.113.7" not in a.host + a.message


def test_authorization_in_referrer_is_redacted_in_raw():
    text = line(combined=' "Authorization: Bearer xyz987" "agent"')
    ev = parse_ncsa(text, CTX).event
    assert "xyz987" not in ev.raw and "<redacted>" in ev.raw


def test_raw_drops_request_and_referer_queries_but_keeps_the_rest():
    text = line("GET /a?id=7&email=a@b.c HTTP/1.1", 404, combined=' "http://h/p?q=1" "UA/1.0"')
    raw = parse_ncsa(text, CTX).event.raw
    for bad in ("?", "id=7", "email", "q=1"):
        assert bad not in raw
    for good in ("203.0.113.7", "404", "UA/1.0", "10/Oct/2026"):
        assert good in raw


def test_ipv6_client_and_dash_size_parse():
    text = '2001:db8::1 - - [10/Oct/2026:13:55:36 +0000] "GET / HTTP/1.0" 200 -'
    out = parse_ncsa(text, CTX)
    assert out.matched and out.event.message == "GET / -> 200"


def test_non_ncsa_line_is_plain_and_unmatched():
    out = parse_ncsa("this is not an access log", CTX)
    assert not out.matched
    expected = parse_plain("this is not an access log", CTX).event
    assert dataclasses.replace(out.event, ts=0) == dataclasses.replace(expected, ts=0)


def test_blank_line_is_skipped():
    assert parse_ncsa("", CTX).event is None


def test_three_client_ips_make_one_signature():
    sigs = set()
    for client in ("203.0.113.7", "198.51.100.1", "192.0.2.44"):
        for _ in range(3):
            ev = parse_ncsa(line("GET /orders/42 HTTP/1.1", 500, client=client), CTX).event
            sigs.add((ev.host, signature(ev.message)))
    assert len(sigs) == 1
