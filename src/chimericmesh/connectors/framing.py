"""Frame decoder for syslog over TCP: RFC 6587 octet counting or newline framing. Pure, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["octet", "lf"]

_DIGITS = b"0123456789"


@dataclass(frozen=True, slots=True)
class FrameError:
    reason: Literal["bad_length", "too_long", "zero_length"]


def _leading_digits(buf: bytes, limit: int = 5) -> int:
    n = 0
    while n < len(buf) and n < limit and buf[n] in _DIGITS:
        n += 1
    return n


def detect_mode(buf: bytes) -> Mode | None:
    """``octet`` for 1 to 4 digits and a space, ``lf`` for any other start, ``None`` while undecided."""
    if not buf:
        return None
    n = _leading_digits(buf)
    if n >= 5 or n == 0:
        return "lf"
    if n == len(buf):
        return None
    return "octet" if buf[n:n + 1] == b" " else "lf"


class FrameDecoder:
    def __init__(self, max_frame: int = 8192) -> None:
        self.max_frame = max_frame
        self.mode: Mode | None = None
        self.failed = False
        self._buf = b""        # octet mode: undecoded bytes; undecided: the bytes seen so far
        self._line = b""       # lf mode: the unfinished line
        self._discard = False  # lf mode: skipping the rest of an overlong line

    def feed(self, data: bytes) -> list[bytes | FrameError]:
        if self.failed:
            return []
        if self.mode is None:
            data = self._buf + data
            self._buf = b""
            self.mode = detect_mode(data)
            if self.mode is None:
                self._buf = data
                return []
        if self.mode == "octet":
            return self._feed_octet(data)
        return list(self._feed_lf(data))

    def finish(self) -> list[bytes]:
        if self.failed:
            return []
        if self.mode is None:
            if not self._buf:
                return []
            self.mode = "lf"
            data, self._buf = self._buf, b""
            self._feed_lf(data)
        if self.mode != "lf" or self._discard:
            return []
        line, self._line = self._line.rstrip(b"\r")[: self.max_frame], b""
        return [line] if line else []

    def _fail(self, out: list, reason: str) -> list:
        self.failed = True
        self._buf = b""
        out.append(FrameError(reason))  # type: ignore[arg-type]
        return out

    def _feed_octet(self, data: bytes) -> list[bytes | FrameError]:
        buf = self._buf + data
        out: list[bytes | FrameError] = []
        pos, size = 0, len(buf)
        while True:
            while pos < size and buf[pos] in b"\r\n":
                pos += 1
            if pos >= size:
                break
            n = _leading_digits(buf[pos:pos + 6], 5)
            if n == 0 or n >= 5:
                return self._fail(out, "bad_length")
            if pos + n >= size:
                break  # header incomplete
            if buf[pos + n] != 0x20:
                return self._fail(out, "bad_length")
            length = int(buf[pos:pos + n])
            if length == 0:
                return self._fail(out, "zero_length")
            if length > self.max_frame:
                return self._fail(out, "too_long")
            start = pos + n + 1
            if start + length > size:
                break  # payload incomplete
            out.append(buf[start:start + length])
            pos = start + length
        self._buf = buf[pos:]
        return out

    def _feed_lf(self, data: bytes) -> list[bytes | FrameError]:
        out: list[bytes | FrameError] = []
        pos, size, limit = 0, len(data), self.max_frame
        while pos < size:
            idx = data.find(b"\n", pos)
            segment = data[pos:] if idx == -1 else data[pos:idx]
            pos = size if idx == -1 else idx + 1
            has_newline = idx != -1
            if self._discard:
                if has_newline:
                    self._discard = False
                continue
            line = self._line + segment[: limit + 1 - len(self._line)] if len(self._line) <= limit else self._line
            if has_newline:
                self._line = b""
                line = line.rstrip(b"\r")[:limit]
                if line:
                    out.append(line)
            elif len(line) >= limit:
                self._line = b""
                self._discard = True
                out.append(line[:limit])
            else:
                self._line = line
        return out
