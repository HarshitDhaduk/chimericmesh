"""IRC (RFC 1459/2812) line parsing and safe outbound message formatting."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from ..events import Event, extract_host, infer_severity

MAX_IRC_LINE = 512  # bytes, including trailing CRLF
_CRLF_NUL = re.compile(r"[\r\n\x00]+")


@dataclass(frozen=True)
class IrcMessage:
    prefix: str
    command: str
    params: list[str]


def parse_irc_line(line: str) -> IrcMessage | None:
    """Split a raw IRC line into prefix/command/params. Returns None for empty input."""
    text = line.strip("\r\n").strip()
    if not text:
        return None

    prefix = ""
    if text.startswith(":"):
        prefix, _, text = text[1:].partition(" ")
        text = text.lstrip()
    if not text:
        return None

    head, sep, trailing = text.partition(" :")
    parts = head.split()
    if not parts:
        return None
    command, params = parts[0].upper(), parts[1:]
    if sep:
        params.append(trailing)
    return IrcMessage(prefix=prefix, command=command, params=params)


def irc_to_event(line: str) -> Event | None:
    """Turn a PRIVMSG/NOTICE into an Event; other commands carry no log content."""
    msg = parse_irc_line(line)
    if msg is None or msg.command not in {"PRIVMSG", "NOTICE"} or len(msg.params) < 2:
        return None
    nick = msg.prefix.split("!", 1)[0] or "unknown"
    text = msg.params[-1]
    host, message = extract_host(text)
    return Event(
        source="irc",
        host=host or nick,
        app=msg.params[0],
        severity=infer_severity(text),
        message=message,
        raw=line.strip("\r\n"),
        ts=time.time(),
    )


def format_privmsg(target: str, text: str) -> list[str]:
    """Build PRIVMSG lines that are injection-safe and fit within 512 bytes each.

    CR/LF/NUL are stripped from both target and text so callers cannot smuggle extra
    IRC commands; long text is split on character boundaries.
    """
    target = _CRLF_NUL.sub("", target).replace(" ", "").replace(",", "") or "*"
    text = _CRLF_NUL.sub(" ", text).strip()
    prefix = f"PRIVMSG {target} :"
    budget = MAX_IRC_LINE - 2 - len(prefix.encode())
    if budget < 8:
        return []

    lines: list[str] = []
    chunk, size = [], 0
    for ch in text:
        n = len(ch.encode())
        if size + n > budget:
            lines.append(prefix + "".join(chunk))
            chunk, size = [], 0
        chunk.append(ch)
        size += n
    if chunk:
        lines.append(prefix + "".join(chunk))
    return lines
