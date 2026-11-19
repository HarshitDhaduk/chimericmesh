"""``syslog-text`` format: syslog-style lines in a file, with or without a PRI header."""
from __future__ import annotations

import re

from ..events import infer_severity
from .logctx import FormatContext, ParseOutcome, clean_line, make_file_event, syslog_text_host
from .plain import plain_outcome
from .syslog import _RFC3164, _RFC5424, parse_syslog

_PRI = re.compile(r"^<(\d{1,3})>")
_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?) (?P<host>\S+) "
    r"(?P<app>[^\s:\[]+)(?:\[\d+\])?:? ?(?P<msg>.*)$",
    re.S,
)


def _match(text: str) -> tuple[str, str, str, bool] | None:
    """(host, app, message, is_iso) from the first pattern that fits, else None."""
    m = _ISO.match(text)
    if m:
        return m["host"], m["app"], m["msg"], True
    m3 = _RFC3164.match(text)
    if m3:
        return m3["host"], m3["app"], m3["msg"], False
    m5 = _RFC5424.match(text)
    if m5:
        host = "" if m5["host"] == "-" else m5["host"]
        app = "" if m5["app"] == "-" else m5["app"]
        return host, app, m5["msg"], False
    return None


def parse_syslog_text(line: str, ctx: FormatContext) -> ParseOutcome:
    text = clean_line(line)
    if not text.strip():
        return ParseOutcome(None, True)
    pri = _PRI.match(text)
    body = text[pri.end():] if pri and int(pri.group(1)) <= 191 else text
    found = _match(body)
    if found is None:
        return plain_outcome(line, ctx, False)
    host, app, message, is_iso = found
    message = message.strip()
    if is_iso:
        severity = int(pri.group(1)) % 8 if body is not text else infer_severity(message, 6)
    else:
        severity = parse_syslog(text, default_host="").severity
    if "status=deferred" in message:
        severity = min(severity, 4)
    if "status=bounced" in message or "status=rejected" in message:
        severity = min(severity, 3)
    event = make_file_event(
        ctx,
        severity=severity,
        message=message,
        raw=text,
        host=syslog_text_host(host.strip(), ctx),
        app=app.strip() or None,
    )
    return ParseOutcome(event, True)
