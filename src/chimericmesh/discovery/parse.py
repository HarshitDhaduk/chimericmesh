"""Parsers of the OS tools' text output. Pure and total: they never raise, skip lines they do not
understand and return at most 20000 records. Process names are untrusted and are normalized."""
from __future__ import annotations

import csv
import re
from collections.abc import Callable, Iterator

from .models import Listener, ProcEntry
from .text import normalize_process_name

MAX_RECORDS = 20000
MAX_LINE = 4096
_USERS = re.compile(r'\("((?:[^"\\]|\\.)*)",pid=(\d+)')
_NETSTAT_PID = re.compile(r"\s(\d+)/(\S+)[^\n]*$")
_PS_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
_LSOF_PORT = re.compile(r"(?:\]|[\w.*%-]):(\d+)(?=\s|-|$)")


def _lines(text: object) -> Iterator[str]:
    if not isinstance(text, str):
        return
    for line in text.splitlines():
        yield line[:MAX_LINE]


def _port(addr: str) -> int | None:
    tail = addr.rsplit(":", 1)[-1]
    if not tail.isdigit() or len(tail) > 5:
        return None
    value = int(tail)
    return value if 1 <= value <= 65535 else None


def _pid(text: str) -> int | None:
    if not text.isdigit() or len(text) > 10:
        return None
    return int(text)


def _proto(word: str) -> str | None:
    low = word.lower()
    if low.startswith("tcp"):
        return "tcp"
    if low.startswith("udp"):
        return "udp"
    return None


def _safe(fn: Callable[[str], object]) -> Callable[[str], tuple]:
    """Wrap a line-level collector so one hostile line cannot break the parse."""
    def run(text: str) -> tuple:
        out: list = []
        for line in _lines(text):
            if len(out) >= MAX_RECORDS:
                break
            try:
                found = fn(line)
            except Exception:  # noqa: BLE001 - parsers never raise
                continue
            if found:
                out.extend(found)
        return tuple(out[:MAX_RECORDS])
    return run


def _ss_line(line: str) -> list[Listener]:
    cols = line.split()
    if len(cols) < 5:
        return []
    proto = _proto(cols[0])
    port = _port(cols[4])
    if proto is None or port is None:
        return []
    found = _USERS.findall(line)
    if not found:
        return [Listener(port, proto, None, None)]       # type: ignore[arg-type]
    out = []
    for name, pid in found:
        out.append(Listener(port, proto, _pid(pid), normalize_process_name(name, "linux") or None))  # type: ignore[arg-type]
    return out


def _netstat_linux_line(line: str) -> list[Listener]:
    cols = line.split()
    if len(cols) < 4:
        return []
    proto = _proto(cols[0])
    port = _port(cols[3])
    if proto is None or port is None:
        return []
    match = _NETSTAT_PID.search(line)
    if match is None:
        return [Listener(port, proto, None, None)]       # type: ignore[arg-type]
    name = normalize_process_name(match.group(2).rstrip(":"), "linux") or None
    return [Listener(port, proto, _pid(match.group(1)), name)]  # type: ignore[arg-type]


def _netstat_windows_line(line: str) -> list[Listener]:
    cols = line.split()
    if len(cols) < 4:
        return []
    kind = cols[0].upper()
    pid = _pid(cols[-1])
    if pid is None:
        return []
    port = _port(cols[1])
    if port is None:
        return []
    if kind == "TCP":
        if not cols[2].endswith(":0"):                   # listening: foreign ends in ":0" in any language
            return []
        return [Listener(port, "tcp", pid, None)]
    if kind == "UDP":
        return [Listener(port, "udp", pid, None)]
    return []


def _ps_line(line: str) -> list[ProcEntry]:
    match = _PS_LINE.match(line)
    if match is None:
        return []
    name = normalize_process_name(match.group(2), "macos")
    return [ProcEntry(_pid(match.group(1)), name)] if name else []


def _lsof_line(line: str) -> list[Listener]:
    cols = line.split()
    if len(cols) < 9:
        return []
    pid = _pid(cols[1])
    proto = _proto(cols[7])
    if pid is None or proto is None:
        return []
    match = _LSOF_PORT.search(" ".join(cols[8:]))
    if match is None:
        return []
    port = _port(":" + match.group(1))
    name = normalize_process_name(cols[0].replace("\\x20", " "), "macos") or None
    return [Listener(port, proto, pid, name)] if port is not None else []  # type: ignore[arg-type]


parse_ss = _safe(_ss_line)
parse_netstat_linux = _safe(_netstat_linux_line)
parse_netstat_windows = _safe(_netstat_windows_line)
parse_ps = _safe(_ps_line)
parse_lsof = _safe(_lsof_line)


def parse_tasklist(text: str) -> tuple[ProcEntry, ...]:
    """``tasklist /fo csv /nh``: column 0 image name, column 1 pid."""
    out: list[ProcEntry] = []
    for line in _lines(text):
        if len(out) >= MAX_RECORDS:
            break
        try:
            row = next(csv.reader([line]), [])
            if len(row) < 2:
                continue
            pid = _pid(row[1].strip())
            name = normalize_process_name(row[0], "windows")
            if pid is not None and name:
                out.append(ProcEntry(pid, name))
        except Exception:  # noqa: BLE001 - csv.Error and anything else: skip the line
            continue
    return tuple(out)
