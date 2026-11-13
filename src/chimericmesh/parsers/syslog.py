"""Syslog parser covering RFC 3164 (BSD) and RFC 5424 line formats.

``parse_syslog`` never raises: anything it cannot understand becomes an info-level
event whose message is the (sanitized) input.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from ..events import Event

_PRI = re.compile(r"^<(\d{1,3})>")
_RFC5424 = re.compile(
    r"^1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<proc>\S+) (?P<msgid>\S+) "
    r"(?P<sd>-|(?:\[[^\]]*\])+) ?(?P<msg>.*)$",
    re.S,
)
_RFC3164 = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) (?P<host>\S+) "
    r"(?P<app>[^\s:\[]+)(?:\[\d+\])?:? ?(?P<msg>.*)$",
    re.S,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MAX_LINE = 8192


def _clean(text: str) -> str:
    return _CONTROL.sub("", text).strip()


def _ts_5424(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_syslog(line: str, default_host: str = "unknown") -> Event:
    raw = line[:MAX_LINE]
    text = raw.strip("\r\n")
    severity, facility = 6, 1

    pri = _PRI.match(text)
    if pri and int(pri.group(1)) <= 191:
        value = int(pri.group(1))
        facility, severity = divmod(value, 8)
        text = text[pri.end():]

    ts: float | None = None
    host, app, message = default_host, "syslog", text

    m5 = _RFC5424.match(text)
    if m5:
        host = m5["host"] if m5["host"] != "-" else default_host
        app = m5["app"] if m5["app"] != "-" else "syslog"
        message = m5["msg"]
        ts = _ts_5424(m5["ts"])
    else:
        m3 = _RFC3164.match(text)
        if m3:
            host, app, message = m3["host"], m3["app"], m3["msg"]

    return Event(
        source="syslog",
        host=_clean(host) or default_host,
        app=_clean(app) or "syslog",
        severity=severity,
        message=_clean(message),
        raw=_clean(raw),
        ts=ts if ts is not None else time.time(),
    )
