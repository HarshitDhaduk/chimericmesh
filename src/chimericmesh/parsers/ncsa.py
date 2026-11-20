"""``ncsa`` format: Common and Combined Log Format."""
from __future__ import annotations

import re

from .logctx import FormatContext, ParseOutcome, clean_line, http_severity, make_file_event
from .plain import plain_outcome

_NCSA = re.compile(
    r'^(\S+) (\S+) (\S+) \[([^\]]{1,64})\] "([^"]{0,8192})" (\d{3}) (\S+)'
    r'(?: "([^"]{0,2048})" "([^"]{0,2048})")?'
)
_REQUEST = re.compile(r"^([A-Za-z]{1,20}) (\S+)(?: HTTP/\d(?:\.\d)?)?$")
_QUERY = re.compile(r"[?#]\S*")


def _cut_query(text: str) -> str:
    """Drop everything from the first ``?`` or ``#`` to the end of that token."""
    return _QUERY.sub("", text)


def _cut_url(text: str) -> str:
    for i, ch in enumerate(text):
        if ch in "?#":
            return text[:i]
    return text


def _message(request: str, status: str) -> str:
    safe = _cut_query(request)
    m = _REQUEST.match(request)
    if m:
        return f"{m.group(1)} {_cut_url(m.group(2))} -> {status}"
    return f'"{safe[:100]}" -> {status}'


def parse_ncsa(line: str, ctx: FormatContext) -> ParseOutcome:
    text = clean_line(line)
    if not text.strip():
        return ParseOutcome(None, True)
    m = _NCSA.match(text)
    if m is None:
        return plain_outcome(line, ctx, False)
    request, status = m.group(5), m.group(6)
    raw = text
    if m.group(8) is not None:
        s, e = m.span(8)
        raw = raw[:s] + _cut_url(m.group(8)) + raw[e:]
    s, e = m.span(5)
    raw = raw[:s] + _cut_query(request) + raw[e:]
    event = make_file_event(
        ctx,
        severity=http_severity(int(status)),
        message=_message(request, status),
        raw=raw,
    )
    return ParseOutcome(event, True)
