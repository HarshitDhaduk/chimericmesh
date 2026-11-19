"""Shared pieces of the file-line formats (group ``core``: stdlib and ``events`` only).

Every format is a pure pair ``update(line, ctx) -> ctx`` and ``parse_line(line, ctx) -> ParseOutcome``.
The tailer owns the immutable :class:`FormatContext` and carries it from line to line.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..events import Event

MAX_MESSAGE = 4096
MAX_RAW = 1024
MAX_HOST = 191
_MACHINE_MAX = 100
_SERVICE_MAX = 40
_LINE_HOST_MAX = 150
_APP_MAX = 100
UNASSIGNED = "unassigned"

_CONTROLS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# One bounded negated class per branch and no nested quantifier: no catastrophic backtracking.
_SECRET = re.compile(
    r"(?i)\b(authorization)\s*:\s*(?:[A-Za-z]+\s+)?[^\s\"']{1,512}"
    r"|\b(password|passwd|pwd|pass|secret|token|access[_-]?token|api[_-]?key|apikey|sessionid)"
    r"\s*[=:]\s*(?:\"[^\"]{0,512}\"|'[^']{0,512}'|[^\s&;,\"']{1,512})"
)


@dataclass(frozen=True, slots=True)
class FormatContext:
    machine: str = "localhost"                    # socket.gethostname()
    service_id: str | None = None                 # None or "unassigned" = no suffix
    source_host: str | None = None                # LogSource.host
    app: str = "file"                             # first app_names entry, else the file's base name
    w3c_fields: tuple[str, ...] | None = None     # column names from the last "#Fields:" line


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    event: Event | None     # None = skip this line (comment, blank)
    matched: bool           # False = the line did not fit the declared format (R4.8)


UpdateFn = Callable[[str, FormatContext], FormatContext]
ParseFn = Callable[[str, FormatContext], ParseOutcome]


@dataclass(frozen=True, slots=True)
class LineFormat:
    name: str
    update: UpdateFn
    parse_line: ParseFn

    def parse(self, line: str, ctx: FormatContext) -> Event | None:
        """The requirement's ``(line, context) -> Event | None``."""
        return self.parse_line(line, ctx).event


def clean_line(text: str) -> str:
    """Remove NUL, CR, LF and the other controls; tab stays."""
    return _CONTROLS.sub("", text)


def truncate(text: str, limit: int = MAX_MESSAGE) -> str:
    return text[:limit]


def _redact_one(match: re.Match[str]) -> str:
    if match.group(1):
        return f"{match.group(1)}: <redacted>"
    return f"{match.group(2)}=<redacted>"


_KEYWORDS = ("authorization", "pass", "pwd", "secret", "token", "key", "sessionid")


def redact(text: str) -> str:
    # A substring pre-check is much cheaper than running the regex at every position of a huge line.
    low = text.lower()
    if not any(word in low for word in _KEYWORDS):
        return text
    return _SECRET.sub(_redact_one, text)


def http_severity(status: int) -> int:
    if 500 <= status <= 599:
        return 3
    if status in (408, 429):
        return 4
    if 400 <= status <= 499:
        return 5
    return 6


def _suffix(ctx: FormatContext) -> str:
    sid = ctx.service_id
    if sid and sid != UNASSIGNED:
        return "/" + sid[:_SERVICE_MAX]
    return ""


def file_host(ctx: FormatContext) -> str:
    """Rule 4.10: the source's host, else ``<machine>[/<service-id>]``."""
    if ctx.source_host:
        return ctx.source_host[:MAX_HOST]
    machine = ctx.machine[:_MACHINE_MAX] or "localhost"
    return (machine + _suffix(ctx))[:MAX_HOST]


def syslog_text_host(line_host: str, ctx: FormatContext) -> str:
    """Rule 4.12: ``<line host>[/<service-id>]``, or rule 4.10 for a line without a host."""
    if not line_host:
        return file_host(ctx)
    return (line_host[:_LINE_HOST_MAX] + _suffix(ctx))[:MAX_HOST]


def make_file_event(
    ctx: FormatContext,
    *,
    severity: int,
    message: str,
    raw: str,
    host: str | None = None,
    app: str | None = None,
) -> Event:
    """Build a ``source="file"`` event: cleaned, redacted, bounded, never an empty host or app."""
    sev = min(7, max(0, int(severity)))
    return Event(
        source="file",
        host=(host or file_host(ctx) or "unknown")[:MAX_HOST],
        app=(app or ctx.app or "file")[:_APP_MAX],
        severity=sev,
        message=truncate(redact(clean_line(message)), MAX_MESSAGE),
        raw=truncate(redact(clean_line(raw)), MAX_RAW),
    )


def identity_update(line: str, ctx: FormatContext) -> FormatContext:
    return ctx
