"""Telnet byte-stream cleaning: strips IAC negotiation and ANSI escapes."""

from __future__ import annotations

import re
import time

from ..events import Event, extract_host, infer_severity

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_telnet(data: bytes) -> str:
    """Remove Telnet IAC commands/subnegotiations and ANSI codes, return clean text."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break  # dangling IAC
        cmd = data[i + 1]
        if cmd == IAC:  # escaped 0xFF data byte
            out.append(IAC)
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            i += 3
        elif cmd == SB:  # skip until IAC SE
            end = data.find(bytes([IAC, SE]), i + 2)
            i = n if end == -1 else end + 2
        else:
            i += 2
    text = bytes(out).decode("utf-8", errors="replace")
    text = _ANSI.sub("", text)
    return _CONTROL.sub("", text.replace("\r", ""))


def telnet_to_event(data: bytes, host: str = "telnet-peer") -> Event | None:
    text = strip_telnet(data).strip()
    if not text:
        return None
    parsed_host, message = extract_host(text)
    return Event(
        source="telnet",
        host=parsed_host or host,
        app="telnet",
        severity=infer_severity(text),
        message=message,
        raw=text,
        ts=time.time(),
    )
