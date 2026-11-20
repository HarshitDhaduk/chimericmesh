"""The command runner: real child processes through ``run_argv`` (R2.1, R2.4, R2.5). Sub-criteria 2.1f, 2.1g, 2.1h."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from chimericmesh.discovery.models import ToolSpec
from chimericmesh.discovery.runner import SubprocessRunner, resolve_tool

PY = sys.executable


def run(code_or_args, *, timeout=10.0, max_bytes=1_000_000, cancel=None, args=()):
    runner = SubprocessRunner("windows" if os.name == "nt" else "linux", dict(os.environ))
    argv = [PY, "-c", code_or_args, *args]
    return runner.run_argv(argv, timeout=timeout, max_bytes=max_bytes, cancel=cancel or threading.Event(),
                           env=dict(os.environ))


def test_2_1g_a_sleeping_child_is_killed_at_the_timeout(tmp_path):
    marker = tmp_path / "alive"
    code = f"import time; time.sleep(2.0); open({str(marker)!r}, 'w').write('x')"
    start = time.monotonic()
    res = run(code, timeout=0.5)
    assert res.reason == "timeout" and not res.ok
    assert time.monotonic() - start < 4.0           # generous: process start-up is slow on a loaded machine
    time.sleep(2.5)
    assert not marker.exists()                       # the child was killed, it never got to write


def test_2_1h_output_is_capped_and_the_child_is_stopped(tmp_path):
    marker = tmp_path / "alive"
    code = (f"import sys,time\nsys.stdout.write('x'*5_000_000); sys.stdout.flush()\n"
            f"time.sleep(2.0); open({str(marker)!r}, 'w').write('x')")
    res = run(code, max_bytes=1000)
    assert res.ok and res.truncated and 0 < len(res.stdout) <= 1000
    time.sleep(2.5)
    assert not marker.exists()


def test_exact_cap_is_not_truncated():
    res = run("import sys; sys.stdout.write('y'*100)", max_bytes=100)
    assert res.ok and not res.truncated and res.stdout == "y" * 100


def test_2_1f_shell_metacharacters_reach_the_child_literally():
    args = ["a;b", "&& echo x", "$(whoami)", "`id`", "|", ">out"]
    res = run("import sys, json; print(json.dumps(sys.argv[1:]))", args=args)
    assert res.ok and json.loads(res.stdout) == args


def test_missing_executable_is_reported_as_missing():
    runner = SubprocessRunner("linux", {})
    res = runner.run_argv(["Z:\\nope\\nothing.exe" if os.name == "nt" else "/nope/nothing"], timeout=2,
                          max_bytes=100, cancel=threading.Event(), env={})
    assert res.reason == "missing" and not res.ok


def test_non_executable_file_does_not_raise(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("hello", encoding="utf-8")
    runner = SubprocessRunner("linux", {})
    res = runner.run_argv([str(f)], timeout=2, max_bytes=100, cancel=threading.Event(), env={})
    assert not res.ok and res.reason in ("denied", "error")


def test_cancel_kills_the_child_quickly():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    res = run("import time; time.sleep(30)", cancel=cancel)
    assert res.reason == "cancelled" and not res.ok
    assert time.monotonic() - start < 4.0


def test_stdin_is_closed():
    res = run("import sys; print(repr(sys.stdin.read()))", timeout=5)
    assert res.ok and res.stdout.strip() == "''"


def test_non_zero_exit_with_output_is_accepted_and_without_output_is_an_error():
    res = run("import sys; print('data'); sys.exit(1)")
    assert res.ok and res.returncode == 1 and "data" in res.stdout
    res = run("import sys; sys.exit(3)")
    assert not res.ok and res.reason == "error" and res.returncode == 3


def test_output_is_decoded_with_replacement():
    res = run("import sys; sys.stdout.buffer.write(b'ok\\xff\\xfe')")
    assert res.ok and res.stdout.startswith("ok")


def test_child_environment_is_minimal():
    for os_name in ("windows", "linux", "macos"):
        env = SubprocessRunner(os_name, {"SECRET": "s", "SystemRoot": "C:\\Windows"})._child_env()
        assert set(env) <= {"SystemRoot", "LC_ALL", "PATH"}
        assert "SECRET" not in env and env["LC_ALL"] == "C"


# ---- resolve_tool --------------------------------------------------------------------------------

def test_resolve_tool_uses_fixed_system_directories_only(tmp_path, monkeypatch):
    for name in ("netstat.exe", "ss", "tasklist.exe", "netstat"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    got = resolve_tool("netstat", "windows", {"PATH": str(tmp_path)}, exists=lambda p: p.lower().endswith("netstat.exe"))
    assert got is not None and not got.lower().startswith(str(tmp_path).lower())
    assert resolve_tool("ss", "linux", {"PATH": str(tmp_path)}, exists=lambda p: p == str(tmp_path / "ss")) is None
    assert resolve_tool("ss", "linux", {}, exists=lambda p: p == "/usr/bin/ss") == "/usr/bin/ss"
    assert resolve_tool("ss", "linux", {}, exists=lambda p: p in ("/bin/ss", "/usr/sbin/ss")) == "/usr/sbin/ss"
    assert resolve_tool("ss", "linux", {}, exists=lambda p: False) is None


def test_resolve_tool_unknown_name_and_hostile_names():
    for name in ("calc", "ss;id", "..\\x", "", "netstat.exe", "NETSTAT"):
        assert resolve_tool(name, "windows", {}, exists=lambda p: True) is None
        assert resolve_tool(name, "linux", {}, exists=lambda p: True) is None


def test_resolve_tool_windows_system32():
    assert resolve_tool("tasklist", "windows", {"SystemRoot": "D:\\Win"}, exists=lambda p: True,
                        isdir=lambda p: True) == "D:\\Win\\System32\\tasklist.exe"
    for bad in ({}, {"SystemRoot": "relative"}, {"SystemRoot": "D:\\Win\x00"}):
        assert resolve_tool("tasklist", "windows", bad, exists=lambda p: True,
                            isdir=lambda p: True) == "C:\\Windows\\System32\\tasklist.exe"
    assert resolve_tool("tasklist", "windows", {"SystemRoot": "D:\\Win"}, exists=lambda p: True,
                        isdir=lambda p: False) == "C:\\Windows\\System32\\tasklist.exe"


def test_tool_names_per_os_are_exact():
    ok = lambda p: True  # noqa: E731
    assert [n for n in ("tasklist", "netstat", "ss", "ps", "lsof") if resolve_tool(n, "windows", {}, exists=ok)] == [
        "tasklist", "netstat"]
    assert [n for n in ("tasklist", "netstat", "ss", "ps", "lsof") if resolve_tool(n, "linux", {}, exists=ok)] == [
        "netstat", "ss"]
    assert [n for n in ("tasklist", "netstat", "ss", "ps", "lsof") if resolve_tool(n, "macos", {}, exists=ok)] == [
        "ps", "lsof"]


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real Windows system tools")
def test_real_windows_tools_run_and_parse():
    from chimericmesh.discovery.parse import parse_netstat_windows, parse_tasklist
    runner = SubprocessRunner("windows", dict(os.environ))
    tl = runner.run(ToolSpec("tasklist", ("/fo", "csv", "/nh")), timeout=10, max_bytes=2_000_000,
                    cancel=threading.Event())
    ns = runner.run(ToolSpec("netstat", ("-ano",)), timeout=10, max_bytes=2_000_000, cancel=threading.Event())
    assert tl.ok and ns.ok
    procs = parse_tasklist(tl.stdout)
    assert len(procs) > 5 and any(p.name == "system" for p in procs)
    assert len(parse_netstat_windows(ns.stdout)) > 0
