"""Task 1.4: the ``w3c`` format (IIS extended log files)."""
from __future__ import annotations

import dataclasses
import time

from chimericmesh.parsers.logctx import FormatContext
from chimericmesh.parsers.plain import parse_plain
from chimericmesh.parsers.w3c import parse_w3c, update_w3c

FIELDS = ("#Fields: date time s-ip cs-method cs-uri-stem cs-uri-query s-port cs-username c-ip "
          "sc-status sc-substatus sc-win32-status")
BASE = FormatContext(machine="box", service_id="iis", app="u_ex")


def data(status: str = "200", win32: str = "0", query: str = "q=secret+1", client: str = "203.0.113.9",
         method: str = "GET", stem: str = "/index.aspx") -> str:
    return f"2026-11-18 10:00:00 10.0.0.1 {method} {stem} {query} 80 - {client} {status} 0 {win32}"


def with_fields(fields: str = FIELDS) -> FormatContext:
    return update_w3c(fields, BASE)


def test_fields_directive_sets_columns_and_is_skipped():
    ctx = with_fields()
    assert ctx.w3c_fields is not None and ctx.w3c_fields[3] == "cs-method" and len(ctx.w3c_fields) == 12
    assert parse_w3c(FIELDS, ctx).event is None
    assert parse_w3c(FIELDS, ctx).matched


def test_other_directives_are_skipped_and_leave_fields():
    ctx = with_fields()
    for text in ("#Software: Microsoft Internet Information Services 10.0", "#Version: 1.0", "#Date: 2026-11-18 00:00:00"):
        assert update_w3c(text, ctx) == ctx
        out = parse_w3c(text, ctx)
        assert out.event is None and out.matched


def test_data_line_maps_by_column_name():
    ctx = with_fields()
    out = parse_w3c(data("500"), ctx)
    assert out.matched and out.event.message == "GET /index.aspx -> 500" and out.event.severity == 3
    assert out.event.host == "box/iis" and out.event.app == "u_ex"
    assert "secret" not in out.event.message + out.event.raw and "q=" not in out.event.raw
    assert "203.0.113.9" in out.event.raw


def test_columns_in_another_order():
    ctx = update_w3c("#Fields: sc-status cs-uri-stem cs-method", BASE)
    out = parse_w3c("404 /missing POST", ctx)
    assert out.event.message == "POST /missing -> 404" and out.event.severity == 5


def test_win32_status_makes_severity_at_least_4():
    ctx = with_fields()
    assert parse_w3c(data("200", win32="64"), ctx).event.severity == 4
    assert parse_w3c(data("200", win32="0"), ctx).event.severity == 6
    assert parse_w3c(data("500", win32="64"), ctx).event.severity == 3


def test_second_fields_line_replaces_columns():
    ctx = with_fields()
    ctx2 = update_w3c("#Fields: sc-status cs-method cs-uri-stem", ctx)
    assert ctx2.w3c_fields == ("sc-status", "cs-method", "cs-uri-stem")


def test_data_line_before_fields_is_plain_and_unmatched():
    out = parse_w3c("2026-11-18 error here", BASE)
    expected = parse_plain("2026-11-18 error here", BASE).event
    assert not out.matched
    assert dataclasses.replace(out.event, ts=0) == dataclasses.replace(expected, ts=0)


def test_wrong_column_count_is_plain_and_unmatched():
    out = parse_w3c("a b c", with_fields())
    assert not out.matched and out.event.message == "a b c"


def test_dash_and_plus_values():
    ctx = update_w3c("#Fields: cs-method cs-uri-stem sc-status", BASE)
    out = parse_w3c("GET /a+b+c 200", ctx)
    assert out.event.message == "GET /a b c -> 200"
    out2 = parse_w3c("- - 200", ctx)
    assert out2.matched and out2.event.message == "- - -> 200"


def test_fields_without_status_column_is_plain():
    ctx = update_w3c("#Fields: cs-method cs-uri-stem", BASE)
    out = parse_w3c("GET /x", ctx)
    assert not out.matched


def test_huge_column_line_does_not_hang():
    names = " ".join(f"c{i}" for i in range(100_000))
    start = time.perf_counter()
    ctx = update_w3c("#Fields: " + names, BASE)
    parse_w3c(" ".join("x" for _ in range(100_000)), ctx)
    parse_w3c(" ".join("x" for _ in range(99_999)), ctx)
    assert time.perf_counter() - start < 2


def test_blank_line_is_skipped():
    assert parse_w3c("", with_fields()).event is None
