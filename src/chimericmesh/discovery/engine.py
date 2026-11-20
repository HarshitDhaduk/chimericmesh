"""``run_scan_sync``: collect, match, return an immutable outcome. It runs on the scan thread and touches no
registry, store or hub (review M3): its inputs and its result are immutable. The raw process list is a local
variable that goes out of scope here (R2.7)."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .collect import collect_snapshot
from .match import build_suggestions
from .models import SCAN_TOTAL_SECONDS, ScanInputs, ScanOutcome
from .probe import FsProbe, ProcFs
from .runner import CommandRunner

VISIBILITY_REASON = "some process details were not visible"


def run_scan_sync(inputs: ScanInputs, runner: CommandRunner, procfs: ProcFs, fs: FsProbe,
                  next_id: Callable[[], str], cancel: threading.Event,
                  *, clock: Callable[[], float] = time.monotonic) -> ScanOutcome:
    deadline = clock() + SCAN_TOTAL_SECONDS            # one deadline for both phases (R2.6)
    snapshot = collect_snapshot(inputs.os_name, runner, procfs, is_admin=inputs.is_admin, own_pid=inputs.own_pid,
                                deadline=deadline, cancel=cancel, clock=clock)
    if not snapshot.usable:
        names = ", ".join(snapshot.failed_tools) or inputs.os_name
        return ScanOutcome("failed", True, f"no scan tool could run: {names}", snapshot.failed_tools, ())
    result = build_suggestions(inputs.catalog, snapshot, inputs.os_name, env=inputs.env, fs=fs,
                               covered=inputs.covered, dismissed=inputs.dismissed, next_id=next_id,
                               deadline=deadline, cancel=cancel, clock=clock)
    reasons: list[str] = []
    if snapshot.failed_tools:
        reasons.append("tools failed: " + ", ".join(snapshot.failed_tools))
    elif snapshot.limited:
        reasons.append(VISIBILITY_REASON)
    if result.reason:
        reasons.append(result.reason)
    limited = snapshot.limited or result.limited
    return ScanOutcome("done", limited, "; ".join(reasons), snapshot.failed_tools, result.suggestions)
