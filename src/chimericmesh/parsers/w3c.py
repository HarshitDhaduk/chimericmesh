"""``w3c`` format: W3C extended log files (IIS, some FTP servers)."""
from __future__ import annotations

import dataclasses

from .logctx import FormatContext, ParseOutcome, clean_line, http_severity, make_file_event
from .plain import plain_outcome

_FIELDS = "#Fields:"


def update_w3c(line: str, ctx: FormatContext) -> FormatContext:
    """A ``#Fields:`` line sets the column names; every other line leaves the context alone."""
    text = clean_line(line).strip()
    if text.startswith(_FIELDS):
        return dataclasses.replace(ctx, w3c_fields=tuple(text[len(_FIELDS):].split()))
    return ctx


def _value(text: str) -> str:
    return text.replace("+", " ")


def _int(text: str) -> int | None:
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 9 else None


def parse_w3c(line: str, ctx: FormatContext) -> ParseOutcome:
    text = clean_line(line).strip()
    if not text:
        return ParseOutcome(None, True)
    if text.startswith("#"):
        return ParseOutcome(None, True)
    fields = ctx.w3c_fields
    columns = text.split(" ")
    if not fields or len(columns) != len(fields) or "sc-status" not in fields:
        return plain_outcome(line, ctx, False)
    index = {name: i for i, name in enumerate(fields)}
    status = _int(columns[index["sc-status"]])
    if status is None:
        return plain_outcome(line, ctx, False)
    severity = http_severity(status)
    if "sc-win32-status" in index:
        win32 = _int(columns[index["sc-win32-status"]])
        if win32:
            severity = min(severity, 4)
    method = _value(columns[index["cs-method"]]) if "cs-method" in index else "-"
    stem = _value(columns[index["cs-uri-stem"]]) if "cs-uri-stem" in index else "-"
    safe = list(columns)
    if "cs-uri-query" in index:
        safe[index["cs-uri-query"]] = "-"
    event = make_file_event(
        ctx,
        severity=severity,
        message=f"{method} {stem} -> {status}",
        raw=" ".join(safe),
    )
    return ParseOutcome(event, True)
