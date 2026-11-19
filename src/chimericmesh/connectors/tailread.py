"""One synchronous poll of a log file: identity, rotation, complete lines. Nothing stays open between polls."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, replace
from typing import Literal

from ..parsers.logctx import clean_line

Status = Literal["running", "busy", "missing", "unreadable", "error"]
Reason = Literal["", "attached", "resumed", "rotated", "truncated", "appeared"]

_BUSY_WINERRORS = (32, 33)
_BUSY_ERRNOS = tuple(e for e in (getattr(errno, "EBUSY", None), getattr(errno, "ETXTBSY", None)) if e)
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOT_REGULAR = "not a regular file"
_INVALID_PATH = "invalid path"


@dataclass(frozen=True, slots=True)
class FileIdent:
    dev: int
    ino: int               # 0 when the filesystem has no inode numbers
    head_len: int          # 0 to 256
    head_sha: str          # hex SHA-256 of the first head_len bytes


@dataclass(frozen=True, slots=True)
class TailState:
    ident: FileIdent | None = None
    offset: int = 0                       # bytes consumed, always at a line start
    discarding: bool = False              # skip bytes up to the next newline (after an overlong line)
    mtime_ns: int = 0                     # for the unchanged fast path; not persisted
    attach: Literal["end", "begin"] = "end"


@dataclass(frozen=True, slots=True)
class RawLine:
    data: bytes
    end: int
    truncated: bool
    discarding_after: bool = False


@dataclass(frozen=True, slots=True)
class PollResult:
    status: Status
    error: str
    start: TailState
    state: TailState
    lines: tuple[RawLine, ...]
    reason: Reason
    more: bool
    bytes_read: int


def _result(status: Status, error: str, state: TailState, *, lines: tuple[RawLine, ...] = (),
            start: TailState | None = None, reason: Reason = "", more: bool = False,
            bytes_read: int = 0) -> PollResult:
    return PollResult(status, error, state if start is None else start, state, lines, reason, more, bytes_read)


def _os_failure(exc: OSError, state: TailState) -> PollResult:
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        kept = replace(state, attach="begin") if state.ident is None else state
        return _result("missing", "file not found", kept)
    winerror = getattr(exc, "winerror", None)
    if (isinstance(exc, PermissionError) and winerror in _BUSY_WINERRORS) or exc.errno in _BUSY_ERRNOS:
        return _result("busy", exc.strerror or str(exc), state)
    return _result("unreadable", exc.strerror or str(exc), state)


def decode_line(raw: RawLine) -> str:
    text = clean_line(raw.data.decode("utf-8", "replace"))
    return text + " [truncated]" if raw.truncated else text


def _same_file(ident: FileIdent, dev: int, ino: int) -> bool:
    return ident.dev == dev and (ident.ino == ino or ident.ino == 0 or ino == 0)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Reader:
    """Reads from an open file and counts the bytes its read calls returned."""

    def __init__(self, f) -> None:  # noqa: ANN001
        self.f = f
        self.bytes_read = 0

    def read_at(self, offset: int, n: int) -> bytes:
        if n <= 0:
            return b""
        self.f.seek(offset)
        chunks: list[bytes] = []
        left = n
        while left > 0:
            chunk = self.f.read(left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        self.bytes_read += len(data)
        return data


def _split_lines(data: bytes, base: int, max_line: int) -> list[RawLine]:
    """Complete lines of ``data`` (which starts at file offset ``base`` and ends with a newline)."""
    out: list[RawLine] = []
    pos = base
    for part in data.split(b"\n")[:-1]:
        end = pos + len(part) + 1
        body = part[:-1] if part.endswith(b"\r") else part
        if len(body) > max_line:
            out.append(RawLine(body[:max_line], end, True))
        else:
            out.append(RawLine(body, end, False))
        pos = end
    return out


def _attach_start(rd: _Reader, size: int, max_line: int) -> tuple[int, bool]:
    """Offset to start at on a first attach, and whether the rest of an overlong line must be skipped."""
    window = min(size, max_line)
    if window == 0:
        return 0, False
    data = rd.read_at(size - window, window)
    idx = data.rfind(b"\n")
    if idx != -1:
        return size - window + idx + 1, False
    if size > max_line:
        return size, True
    return 0, False


def _backfill(rd: _Reader, start: int, count: int, max_read: int, max_line: int) -> list[RawLine]:
    if count <= 0 or start <= 0:
        return []
    lo = max(0, start - max_read)
    data = rd.read_at(lo, start - lo)
    if lo > 0:
        cut = data.find(b"\n")
        if cut == -1:
            return []
        lo += cut + 1
        data = data[cut + 1:]
    if not data.endswith(b"\n"):
        return []
    return _split_lines(data, lo, max_line)[-count:]


def _poll(path: str, state: TailState, max_read: int, max_line: int, backfill_lines: int,
          head_size: int) -> PollResult:
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        return _result("error", _NOT_REGULAR, state)
    prev = state.ident
    if (prev is not None and _same_file(prev, st.st_dev, st.st_ino) and st.st_size == state.offset
            and st.st_mtime_ns == state.mtime_ns and not state.discarding):
        return _result("running", "", state)

    fd = os.open(path, _OPEN_FLAGS)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            os.close(fd)
            return _result("error", _NOT_REGULAR, state)
        f = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with f:
        rd = _Reader(f)
        return _poll_open(rd, fst, state, max_read, max_line, backfill_lines, head_size)


def _poll_open(rd: _Reader, fst, state: TailState, max_read: int, max_line: int, backfill_lines: int,
               head_size: int) -> PollResult:  # noqa: ANN001
    size = fst.st_size
    head = rd.read_at(0, head_size)
    prev = state.ident
    reason: Reason = ""
    discarding = False
    backfilled: list[RawLine] = []

    if prev is None:
        if state.attach == "begin":
            start_off, reason = 0, "appeared"
        else:
            start_off, discarding = _attach_start(rd, size, max_line)
            reason = "attached"
            if not discarding:
                backfilled = _backfill(rd, start_off, backfill_lines, max_read, max_line)
    else:
        if _same_file(prev, fst.st_dev, fst.st_ino):
            if size < state.offset:
                start_off, reason = 0, "truncated"
            elif len(head) >= prev.head_len and _sha(head[:prev.head_len]) == prev.head_sha:
                start_off, discarding = state.offset, state.discarding
                reason = "resumed" if state.mtime_ns == 0 else ""
            else:
                start_off, reason = 0, "rotated"
        else:
            start_off, reason = 0, "rotated"

    ident = FileIdent(fst.st_dev, fst.st_ino, len(head), _sha(head))
    start_state = replace(state, ident=ident, offset=start_off, discarding=discarding, attach="end")

    data = rd.read_at(start_off, max_read)
    more = len(data) == max_read
    if more and max_read < max_line and b"\n" not in data:
        # Only when max_read is configured below max_line: read on to max_line so a long line is cut at
        # max_line, not at max_read, and a poll still always makes progress.
        data += rd.read_at(start_off + len(data), max_line - len(data))
    pos = start_off
    if discarding:
        idx = data.find(b"\n")
        if idx == -1:
            final = replace(start_state, offset=start_off + len(data), mtime_ns=fst.st_mtime_ns)
            return PollResult("running", "", start_state, final, tuple(backfilled), reason, more, rd.bytes_read)
        pos += idx + 1
        data = data[idx + 1:]
        discarding = False

    lines = list(backfilled)
    parts = data.split(b"\n")
    lines += _split_lines(data[: len(data) - len(parts[-1])], pos, max_line)
    consumed = pos + (len(data) - len(parts[-1]))
    unfinished = parts[-1]
    if unfinished and len(unfinished) >= max_line:
        end = consumed + len(unfinished)
        lines.append(RawLine(unfinished[:max_line], end, True, True))
        consumed = end
        discarding = True
    final = replace(start_state, offset=consumed, discarding=discarding, mtime_ns=fst.st_mtime_ns)
    return PollResult("running", "", start_state, final, tuple(lines), reason, more, rd.bytes_read)


def poll_file(path: str, state: TailState, *, max_read: int = 1 << 20, max_line: int = 8192,
              backfill_lines: int = 0, head_size: int = 256) -> PollResult:
    """Poll once. Never raises: every failure becomes a status."""
    try:
        return _poll(path, state, max_read, max_line, backfill_lines, head_size)
    except (ValueError, UnicodeError):
        return _result("error", _INVALID_PATH, state)
    except OSError as exc:
        return _os_failure(exc, state)
    except Exception as exc:  # a bug must not kill the tailer task
        return _result("error", f"{type(exc).__name__}: {exc}", state)


def header_fields(path: str, *, size: int = 4096) -> str | None:
    """The last ``#Fields:`` line that ends inside the first ``size`` bytes of the file, else ``None``."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        fd = os.open(path, _OPEN_FLAGS)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb", buffering=0) as f:
                fd = -1
                data = f.read(size)
        finally:
            if fd != -1:
                os.close(fd)
    except (OSError, ValueError, UnicodeError):
        return None
    found: str | None = None
    for raw in data.split(b"\n")[:-1]:
        line = raw.rstrip(b"\r")
        if line.startswith(b"#Fields:"):
            found = line.decode("utf-8", "replace")
    return found
