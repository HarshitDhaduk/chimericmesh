"""Tool plans and snapshot collection with a fake runner (R2.1, R2.4 to R2.7). Sub-criteria 2.1a to 2.1e, 2.5a to 2.5c."""
from __future__ import annotations

import threading

import pytest

from chimericmesh.discovery.collect import collect_snapshot, tool_plan
from chimericmesh.discovery.models import Listener, ProcEntry, RunResult, Snapshot, ToolSpec


class FakeRunner:
    def __init__(self, results: dict[str, RunResult | Exception], on_call=None) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[str, ...], float]] = []
        self.on_call = on_call

    def run(self, tool: ToolSpec, *, timeout: float, max_bytes: int, cancel):
        self.calls.append((tool.name, tool.args, timeout))
        if self.on_call:
            self.on_call(tool)
        res = self.results.get(tool.name, RunResult(False, "", "missing"))
        if isinstance(res, Exception):
            raise res
        return res


class FakeProc:
    def __init__(self, comms: dict[int, str | None]) -> None:
        self.comms = comms

    def pids(self):
        return sorted(self.comms)

    def comm(self, pid):
        return self.comms.get(pid)


def ok(text, **kw):
    return RunResult(True, text, "", **kw)


SS_OK = 'tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6))\ntcp LISTEN 0 1 [::]:21 [::]:*\n'
SS_ALL = 'tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
TASKLIST = '"nginx.exe","4120","Console","1","1 K"\n"w3wp.exe","9000","Services","0","1 K"\n"python.exe","77","x","0","1 K"\n'
NETSTAT_WIN = "  TCP    0.0.0.0:80   0.0.0.0:0   LISTENING   4120\n  TCP    0.0.0.0:21   0.0.0.0:0   LISTENING   77\n"


def collect(os_name, runner, procfs=None, *, admin=False, own=77, deadline=1000.0, now=lambda: 0.0, cancel=None):
    return collect_snapshot(os_name, runner, procfs or FakeProc({}), is_admin=admin, own_pid=own,
                            deadline=deadline, cancel=cancel or threading.Event(), clock=now)


def test_tool_plans_are_the_r2_1_lists_exactly():
    assert tool_plan("linux") == (ToolSpec("ss", ("-ltnup",)), ToolSpec("netstat", ("-ltnup",)))        # 2.1a, 2.1b
    assert tool_plan("windows") == (ToolSpec("tasklist", ("/fo", "csv", "/nh")), ToolSpec("netstat", ("-ano",)))  # 2.1d
    assert tool_plan("macos") == (ToolSpec("ps", ("-axo", "pid=,comm=")),
                                  ToolSpec("lsof", ("-nP", "-iTCP", "-sTCP:LISTEN", "-iUDP")))          # 2.1e
    assert tool_plan("plan9") == ()


def test_2_1b_netstat_runs_only_when_ss_is_missing_errors_or_times_out():
    proc = FakeProc({812: "nginx"})
    r = FakeRunner({"ss": ok(SS_OK), "netstat": ok("")})
    collect("linux", r, proc)
    assert [c[0] for c in r.calls] == ["ss"]
    for bad in (RunResult(False, "", "missing"), RunResult(False, "", "error"), RunResult(False, "", "timeout")):
        r = FakeRunner({"ss": bad, "netstat": ok("tcp 0 0 0.0.0.0:80 0.0.0.0:* LISTEN 812/nginx\n")})
        snap = collect("linux", r, proc)
        assert [c[0] for c in r.calls] == ["ss", "netstat"]
        assert snap.usable and snap.limited and "ss" in snap.failed_tools
        assert Listener(80, "tcp", 812, "nginx") in snap.listeners


def test_2_1c_linux_names_come_from_proc_comm():
    snap = collect("linux", FakeRunner({"ss": ok(SS_OK)}), FakeProc({812: "NGINX", 5: "vsftpd", 6: None}))
    assert ProcEntry(812, "nginx") in snap.procs and ProcEntry(5, "vsftpd") in snap.procs
    assert all(p.pid != 6 for p in snap.procs)


def test_2_5a_missing_tool_is_recorded_others_run_scan_is_limited_and_usable():
    r = FakeRunner({"tasklist": RunResult(False, "", "missing"), "netstat": ok(NETSTAT_WIN)})
    snap = collect("windows", r)
    assert [c[0] for c in r.calls] == ["tasklist", "netstat"]
    assert snap.usable and snap.limited and snap.failed_tools == ("tasklist",)
    assert Listener(80, "tcp", 4120, None) in snap.listeners


def test_2_5b_timed_out_tool_is_treated_the_same():
    r = FakeRunner({"tasklist": ok(TASKLIST), "netstat": RunResult(False, "", "timeout")})
    snap = collect("windows", r)
    assert snap.usable and snap.limited and snap.failed_tools == ("netstat",)
    assert ProcEntry(4120, "nginx") in snap.procs


def test_2_5c_all_tools_failing_is_unusable_and_names_the_tools():
    snap = collect("windows", FakeRunner({}))
    assert not snap.usable and snap.limited and snap.failed_tools == ("tasklist", "netstat")
    snap = collect("linux", FakeRunner({}), FakeProc({}))
    assert not snap.usable and set(snap.failed_tools) >= {"ss", "netstat"}
    snap = collect("plan9", FakeRunner({}))
    assert not snap.usable


def test_2_4_denied_sets_limited_with_exactly_one_call_per_tool_and_no_elevation():
    r = FakeRunner({"tasklist": ok(TASKLIST), "netstat": RunResult(False, "", "denied")})
    snap = collect("windows", r)
    assert snap.limited and snap.failed_tools == ("netstat",)
    assert [c[0] for c in r.calls] == ["tasklist", "netstat"]                    # no retry


def test_a_hostile_runner_exception_is_a_failed_tool_not_a_crash():
    snap = collect("windows", FakeRunner({"tasklist": RuntimeError("boom"), "netstat": ok(NETSTAT_WIN)}))
    assert snap.failed_tools == ("tasklist",) and snap.usable


def test_non_zero_exit_with_output_is_parsed_and_limited():
    snap = collect("windows", FakeRunner({"tasklist": ok(TASKLIST, returncode=1), "netstat": ok(NETSTAT_WIN)}))
    assert snap.limited and not snap.failed_tools and ProcEntry(4120, "nginx") in snap.procs


def test_truncated_output_is_parsed_and_limited():
    snap = collect("windows", FakeRunner({"tasklist": ok(TASKLIST, truncated=True), "netstat": ok(NETSTAT_WIN)}))
    assert snap.limited and ProcEntry(4120, "nginx") in snap.procs


def test_linux_listener_without_process_info_is_limited_unless_admin():
    runner = lambda: FakeRunner({"ss": ok(SS_OK)})  # noqa: E731
    assert collect("linux", runner(), FakeProc({812: "nginx"}), admin=False).limited
    assert not collect("linux", runner(), FakeProc({812: "nginx"}), admin=True).limited
    assert not collect("linux", FakeRunner({"ss": ok(SS_ALL)}), FakeProc({812: "nginx"}), admin=False).limited


def test_macos_is_limited_unless_admin():
    res = {"ps": ok("  1 /sbin/launchd\n"), "lsof": ok("")}
    assert collect("macos", FakeRunner(dict(res)), admin=False).limited
    assert not collect("macos", FakeRunner(dict(res)), admin=True).limited


def test_windows_listener_names_are_joined_from_the_process_list():
    snap = collect("windows", FakeRunner({"tasklist": ok(TASKLIST), "netstat": ok(NETSTAT_WIN)}))
    assert Listener(80, "tcp", 4120, "nginx") in snap.listeners


def test_own_pid_is_dropped_everywhere():
    snap = collect("windows", FakeRunner({"tasklist": ok(TASKLIST), "netstat": ok(NETSTAT_WIN)}), own=77)
    assert all(p.pid != 77 for p in snap.procs) and all(l.pid != 77 for l in snap.listeners)
    assert any(p.pid == 4120 for p in snap.procs)


def test_each_tool_gets_at_most_10_seconds_and_the_remaining_time():
    clock = [0.0]
    r = FakeRunner({"tasklist": ok(TASKLIST), "netstat": ok(NETSTAT_WIN)}, on_call=lambda t: clock.__setitem__(0, clock[0] + 33))
    collect("windows", r, deadline=40.0, now=lambda: clock[0])
    assert r.calls[0][2] == 10.0
    assert r.calls[1][2] == pytest.approx(7.0)


def test_deadline_marks_remaining_tools_failed_and_keeps_earlier_data():
    clock = [0.0]
    r = FakeRunner({"tasklist": ok(TASKLIST), "netstat": ok(NETSTAT_WIN)}, on_call=lambda t: clock.__setitem__(0, 31.0))
    snap = collect("windows", r, deadline=30.0, now=lambda: clock[0])
    assert [c[0] for c in r.calls] == ["tasklist"]
    assert snap.failed_tools == ("netstat",) and snap.limited and snap.usable
    assert ProcEntry(4120, "nginx") in snap.procs


def test_cancel_set_before_start_runs_nothing():
    cancel = threading.Event()
    cancel.set()
    r = FakeRunner({"tasklist": ok(TASKLIST)})
    snap = collect("windows", r, cancel=cancel)
    assert r.calls == [] and not snap.usable


def test_snapshot_is_the_only_holder_of_the_raw_lists():
    snap = collect("windows", FakeRunner({"tasklist": ok(TASKLIST), "netstat": ok(NETSTAT_WIN)}))
    assert isinstance(snap, Snapshot)
    assert not hasattr(snap, "__dict__")                                           # slots only, no stray attributes
