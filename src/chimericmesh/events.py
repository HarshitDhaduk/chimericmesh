"""Normalized event model shared by every legacy protocol parser."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

Source = Literal["syslog", "irc", "telnet", "api", "file"]

# Syslog severities (RFC 5424): 0 emerg ... 7 debug
SEVERITY_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]

_SEVERITY_HINTS: list[tuple[int, re.Pattern[str]]] = [
    (2, re.compile(r"\b(panic|fatal|segfault|kernel bug|out of memory|oom[- ]kill)", re.I)),
    (3, re.compile(r"\b(error|failed|failure|refused|denied|exception|traceback|no space)", re.I)),
    (4, re.compile(r"\b(warn|warning|timeout|timed out|retry|degraded)", re.I)),
]


def infer_severity(message: str, default: int = 6) -> int:
    """Guess a syslog severity from free text (used by IRC and Telnet, which carry none)."""
    for severity, pattern in _SEVERITY_HINTS:
        if pattern.search(message):
            return severity
    return default


_HOST_PREFIX = re.compile(
    r"^\s*(?:(?:ALERT|ERROR|WARN(?:ING)?|CRIT(?:ICAL)?|FATAL)\s+)?"
    r"(?P<host>[A-Za-z0-9][\w.\-]{1,62}):\s+(?P<rest>\S.*)$",
    re.S,
)


def extract_host(text: str) -> tuple[str | None, str]:
    """Split a leading ``hostname:`` off a free-text line (IRC/Telnet carry no host field).

    Only names containing a digit, dash or dot count as hosts, so words like
    ``kernel:`` or ``Warning:`` are left alone. Returns (host or None, message).
    """
    m = _HOST_PREFIX.match(text)
    if m and re.search(r"[\d.\-]", m["host"]):
        return m["host"], m["rest"].strip()
    return None, text.strip()


@dataclass(frozen=True)
class Event:
    source: str
    host: str
    app: str
    severity: int
    message: str
    raw: str = ""
    ts: float = field(default_factory=time.time)
    # Appended after ``ts`` so positional construction keeps working.
    id: int = field(default=0, compare=False)  # assigned by the hub at ingest; 0 = not yet assigned
    service_id: str | None = None
    log_source_id: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.severity <= 7:
            raise ValueError(f"severity out of range: {self.severity}")

    @property
    def severity_name(self) -> str:
        return SEVERITY_NAMES[self.severity]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["severity_name"] = self.severity_name
        return data
