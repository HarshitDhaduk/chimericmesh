"""The scan engine and the guarantees of a scan (R2.2 to R2.7, Success Criteria 1 and 2)."""
from __future__ import annotations

import builtins
import dataclasses
import itertools
import json
import os
import socket
import threading
from pathlib import Path

from chimericmesh.discovery.catalog import load_catalog
from chimericmesh.discovery.engine import run_scan_sync
from chimericmesh.discovery.models import RunResult, ScanInputs, ToolSpec
from chimericmesh.discovery.probe import OsFsProbe

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})


def rule(path, fmt, kind):
    return {"path": path, "format": fmt, "kind": kind}


APPS = [
    {"app_id": "nginx", "name": "nginx", "process_names": ["nginx"], "ports": [80], "protocol_label": "http",
     "justification": "t", "log_paths": {"linux": [rule("$HOME/logs/nginx/access*.log", "ncsa", "access"),
                                                    rule("$HOME/logs/nginx/error.log", "plain", "error")],
                                         "windows": [rule("%ProgramData%\\logs\\nginx\\access*.log", "ncsa", "access"),
                                                     rule("%ProgramData%\\logs\\nginx\\error.log", "plain", "error")],
                                         "macos": []}},
    {"app_id": "vsftpd", "name": "vsftpd", "process_names": ["vsftpd"], "ports": [21], "protocol_label": "ftp",
     "justification": "t", "log_paths": {"linux": [rule("$HOME/logs/vsftpd.log", "xferlog", "ftp")],
                                         "windows": [rule("%ProgramData%\\logs\\vsftpd.log", "xferlog", "ftp")],
                                         "macos": []}},
]
CATALOG = load_catalog(FORMATS, text=json.dumps({"version": 1, "apps": APPS})).catalog
_SS = ('tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
      'tcp LISTEN 0 1 0.0.0.0:21 0.0.0.0:* users:(("vsftpd",pid=900,fd=3))\n'
      'tcp LISTEN 0 1 0.0.0.0:9999 0.0.0.0:* users:(("secretapp",pid=950,fd=3))\n')


class FakeRunner:
    def __init__(self, results, on_call=None):
        self.results, self.on_call, self.calls = results, on_call, []

    def run(self, tool: ToolSpec, *, timeout, max_bytes, cancel):
        self.calls.append(tool.name)
        if self.on_call:
            self.on_call()
        return self.results.get(tool.name, RunResult(False, "", "missing"))


class FakeProc:
    def __init__(self, comms):
        self.comms = comms

    def pids(self):
        return sorted(self.comms)

    def comm(self, pid):
        return self.comms[pid]


PROCS = {812: "nginx", 900: "vsftpd", 950: "secretapp", 951: "othercanary", 1: "systemd"}
HOST = "windows" if os.name == "nt" else "linux"       # the real OsFsProbe needs host-style paths
ENV_NAME = "ProgramData" if HOST == "windows" else "HOME"
_TASKLIST = "".join(f'"{n}.exe","{p}","Console","1","1 K"\n' for p, n in PROCS.items())
_NETSTAT = "".join(f"  TCP    0.0.0.0:{port}   0.0.0.0:0   LISTENING   {pid}\n"
                   for port, pid in ((80, 812), (21, 900), (9999, 950)))


def default_results():
    if HOST == "windows":
        return {"tasklist": RunResult(True, _TASKLIST, ""), "netstat": RunResult(True, _NETSTAT, "")}
    return {"ss": RunResult(True, _SS, "")}


def inputs(home, *, os_name=HOST, own=4242, covered=None, dismissed=frozenset()):
    return ScanInputs(os_name=os_name, is_admin=True, own_pid=own, env={ENV_NAME: str(home)}, catalog=CATALOG,
                      covered=covered or {}, dismissed=dismissed, known_formats=FORMATS)


def scan(home, runner=None, procfs=None, fs=None, **kw):
    counter = itertools.count(1)
    clock = kw.pop("clock", None)
    extra = {"clock": clock} if clock else {}
    return run_scan_sync(inputs(home, **kw), runner or FakeRunner(default_results()),
                         procfs or FakeProc(PROCS), fs or OsFsProbe(), lambda: f"sg{next(counter)}",
                         threading.Event(), **extra)


def make_tree(home: Path):
    (home / "logs" / "nginx").mkdir(parents=True)
    (home / "logs" / "nginx" / "access.log").write_text("SECRET-CONTENT-1\n", encoding="utf-8")
    (home / "logs" / "nginx" / "error.log").write_text("SECRET-CONTENT-2\n", encoding="utf-8")
    (home / "logs" / "vsftpd.log").write_text("SECRET-CONTENT-3\n", encoding="utf-8")


def test_success_criterion_1_two_high_suggestions_and_no_candidate_file_is_opened(tmp_path, monkeypatch):
    make_tree(tmp_path)
    ftp_size = (tmp_path / "logs" / "vsftpd.log").stat().st_size
    real_open = builtins.open
    touched: list[str] = []

    def guarded(file, *a, **k):
        if str(tmp_path) in str(file):
            raise AssertionError(f"open() on a candidate path: {file}")
        return real_open(file, *a, **k)

    real_stat, real_access, real_scandir = os.stat, os.access, os.scandir

    def spy(fn):
        def wrapper(path, *a, **k):
            if str(tmp_path) in str(path):
                touched.append(str(path))
            return fn(path, *a, **k)
        return wrapper

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(os, "stat", spy(real_stat))
    monkeypatch.setattr(os, "access", spy(real_access))
    monkeypatch.setattr(os, "scandir", spy(real_scandir))
    outcome = scan(tmp_path)
    assert outcome.state == "done"
    got = {s.app_id: s for s in outcome.suggestions}
    assert set(got) == {"nginx", "vsftpd"} and all(s.confidence == "high" for s in got.values())
    assert {c.format for c in got["nginx"].candidates} == {"ncsa", "plain"}
    assert got["vsftpd"].candidates[0].format == "xferlog" and got["vsftpd"].candidates[0].size_bytes == ftp_size
    candidates = {c.path for s in outcome.suggestions for c in s.candidates}
    parents = {str(Path(c).parent) for c in candidates}
    assert set(touched) <= candidates | parents | {str(tmp_path / "logs" / "nginx")}
    assert touched


def test_success_criterion_2_scan_completes_with_sockets_disabled(tmp_path, monkeypatch):
    make_tree(tmp_path)

    def no_socket(*a, **k):
        raise AssertionError("a network socket was created")

    monkeypatch.setattr(socket, "socket", no_socket)
    assert scan(tmp_path).state == "done"


def test_2_4_denied_tool_is_limited_and_never_retried(tmp_path):
    runner = FakeRunner({"tasklist": RunResult(True, '"nginx.exe","812","x","0","1 K"\n', ""),
                         "netstat": RunResult(False, "", "denied")})
    outcome = scan(tmp_path, runner=runner, os_name="windows")
    assert outcome.state == "done" and outcome.limited and outcome.failed_tools == ("netstat",)
    assert runner.calls == ["tasklist", "netstat"]


def test_all_tools_failing_is_failed_with_the_tool_names(tmp_path):
    outcome = scan(tmp_path, runner=FakeRunner({}), procfs=FakeProc({}))
    assert outcome.state == "failed" and outcome.suggestions == ()
    assert outcome.reason.startswith("no scan tool could run:") and "netstat" in outcome.reason


def test_2_6_the_tool_phase_stops_at_30_seconds(tmp_path):
    clock = [0.0]
    runner = FakeRunner({"tasklist": RunResult(True, '"nginx.exe","812","x","0","1 K"\n', ""),
                         "netstat": RunResult(True, "", "")}, on_call=lambda: clock.__setitem__(0, 31.0))
    outcome = scan(tmp_path, runner=runner, os_name="windows", clock=lambda: clock[0])
    assert runner.calls == ["tasklist"]
    assert outcome.state == "done" and outcome.limited and outcome.failed_tools == ("netstat",)


class SlowFs(OsFsProbe):
    def __init__(self, clock):
        self.clock = clock

    def stat(self, path):
        self.clock[0] += 20.0
        return super().stat(path)


def test_2_6_the_match_phase_stops_too_with_the_same_deadline(tmp_path):
    make_tree(tmp_path)
    clock = [0.0]
    outcome = scan(tmp_path, fs=SlowFs(clock), clock=lambda: clock[0])
    assert outcome.state == "done" and outcome.limited and "scan time limit" in outcome.reason


def test_2_7_no_unmatched_process_name_survives_the_scan(tmp_path):
    make_tree(tmp_path)
    outcome = scan(tmp_path)
    text = json.dumps(dataclasses.asdict(outcome))
    for canary in ("secretapp", "othercanary", "systemd", "9999"):
        assert canary not in text
    assert "SECRET-CONTENT" not in text


def test_the_hub_never_suggests_itself(tmp_path):
    make_tree(tmp_path)
    outcome = scan(tmp_path, own=812)
    assert {s.app_id for s in outcome.suggestions} == {"vsftpd"}


def test_covered_paths_and_dismissals_are_inputs(tmp_path):
    from chimericmesh.discovery.catalog import fingerprint
    from chimericmesh.discovery.text import path_key
    make_tree(tmp_path)
    ftp = str(tmp_path / "logs" / "vsftpd.log")
    outcome = scan(tmp_path, covered={path_key(ftp, HOST): "svc"})
    assert {s.app_id for s in outcome.suggestions} == {"nginx"}
    entry = next(e for e in CATALOG.entries if e.app_id == "nginx")
    outcome = scan(tmp_path, dismissed=frozenset({("nginx", fingerprint(entry, HOST))}))
    assert {s.app_id for s in outcome.suggestions} == {"vsftpd"}
