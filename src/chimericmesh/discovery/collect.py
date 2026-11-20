"""Run the OS tools and parse their output into an immutable ``Snapshot`` (R2.1, R2.4 to R2.7).

``usable`` is true when at least one tool ran. Everything raw (the process list) lives in the returned
snapshot only; the engine drops it after matching."""
from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable

from .models import (
    MAX_TOOL_OUTPUT_BYTES,
    TOOL_TIMEOUT_SECONDS,
    Listener,
    ProcEntry,
    RunResult,
    Snapshot,
    ToolSpec,
)
from .parse import parse_lsof, parse_netstat_linux, parse_netstat_windows, parse_ps, parse_ss, parse_tasklist
from .probe import ProcFs
from .runner import CommandRunner
from .text import normalize_process_name

SS = ToolSpec("ss", ("-ltnup",))
NETSTAT_LINUX = ToolSpec("netstat", ("-ltnup",))
TASKLIST = ToolSpec("tasklist", ("/fo", "csv", "/nh"))
NETSTAT_WINDOWS = ToolSpec("netstat", ("-ano",))
PS = ToolSpec("ps", ("-axo", "pid=,comm="))
LSOF = ToolSpec("lsof", ("-nP", "-iTCP", "-sTCP:LISTEN", "-iUDP"))
PLANS: dict[str, tuple[ToolSpec, ...]] = {
    "linux": (SS, NETSTAT_LINUX),          # netstat is the fallback: it runs only when ss fails
    "windows": (TASKLIST, NETSTAT_WINDOWS),
    "macos": (PS, LSOF),
}


def tool_plan(os_name: str) -> tuple[ToolSpec, ...]:
    return PLANS.get(os_name, ())


class _Collector:
    def __init__(self, runner: CommandRunner, deadline: float, cancel: threading.Event,
                 clock: Callable[[], float]) -> None:
        self.runner, self.deadline, self.cancel, self.clock = runner, deadline, cancel, clock
        self.failed: list[str] = []
        self.limited = False
        self.ran = False

    def run(self, tool: ToolSpec, parse: Callable[[str], tuple]) -> tuple | None:
        remaining = self.deadline - self.clock()
        if remaining <= 0 or self.cancel.is_set():
            self._fail(tool)
            return None
        timeout = min(TOOL_TIMEOUT_SECONDS, remaining)
        try:
            result: RunResult = self.runner.run(tool, timeout=timeout, max_bytes=MAX_TOOL_OUTPUT_BYTES,
                                                cancel=self.cancel)
        except Exception:  # noqa: BLE001 - a broken runner is a failed tool, never a crashed scan
            result = RunResult(False, "", "error")
        if not result.ok:
            self._fail(tool)
            return None
        self.ran = True
        if result.truncated or (result.returncode not in (0, None)):
            self.limited = True
        return parse(result.stdout)

    def _fail(self, tool: ToolSpec) -> None:
        self.failed.append(tool.name)
        self.limited = True


def _linux_procs(procfs: ProcFs, deadline: float, cancel: threading.Event, clock: Callable[[], float]) -> list[ProcEntry] | None:
    try:
        pids = procfs.pids()
    except Exception:  # noqa: BLE001
        return None
    if not pids:
        return None
    out: list[ProcEntry] = []
    for i, pid in enumerate(pids):
        if i % 200 == 0 and (cancel.is_set() or clock() >= deadline):
            break
        try:
            comm = procfs.comm(pid)
        except Exception:  # noqa: BLE001
            comm = None
        if comm:
            name = normalize_process_name(comm, "linux")
            if name:
                out.append(ProcEntry(pid, name))
    return out


def collect_snapshot(os_name: str, runner: CommandRunner, procfs: ProcFs, *, is_admin: bool, own_pid: int,
                     deadline: float, cancel: threading.Event,
                     clock: Callable[[], float] = time.monotonic) -> Snapshot:
    col = _Collector(runner, deadline, cancel, clock)
    procs: list[ProcEntry] = []
    listeners: list[Listener] = []
    if os_name == "linux":
        found = _linux_procs(procfs, deadline, cancel, clock)
        if found is None:
            col.failed.append("/proc")
            col.limited = True
        else:
            procs = found
            col.ran = True
        got = col.run(SS, parse_ss)
        if got is None:
            got = col.run(NETSTAT_LINUX, parse_netstat_linux)
        listeners = list(got or ())
        if not is_admin and any(l.pid is None for l in listeners):
            col.limited = True
    elif os_name == "windows":
        procs = list(col.run(TASKLIST, parse_tasklist) or ())
        listeners = list(col.run(NETSTAT_WINDOWS, parse_netstat_windows) or ())
    elif os_name == "macos":
        procs = list(col.run(PS, parse_ps) or ())
        listeners = list(col.run(LSOF, parse_lsof) or ())
        if not is_admin:
            col.limited = True          # non-root lsof lists only the user's own sockets
    names = {p.pid: p.name for p in procs if p.pid is not None}
    joined = [dataclasses.replace(l, name=names[l.pid]) if l.name is None and l.pid in names else l
              for l in listeners]
    procs = [p for p in procs if p.pid != own_pid]
    joined = [l for l in joined if l.pid != own_pid]
    return Snapshot(procs=tuple(procs), listeners=tuple(joined), limited=col.limited,
                    failed_tools=tuple(col.failed), usable=col.ran)
