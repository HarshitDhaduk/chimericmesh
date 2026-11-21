"""Probes: stat, access, entry names and /proc comm (R2.2)."""
from __future__ import annotations

import builtins
import os
import sys

import pytest

from chimericmesh.discovery.probe import LinuxProcFs, OsFsProbe


def test_stat_missing_file_and_directory(tmp_path):
    probe = OsFsProbe()
    f = tmp_path / "a.log"
    f.write_bytes(b"12345")
    exists, size, mtime = probe.stat(str(f))
    assert exists and size == 5 and mtime and mtime > 0
    assert probe.stat(str(tmp_path / "nope.log")) == (False, None, None)
    exists, size, _ = probe.stat(str(tmp_path))
    assert exists and size is None
    assert probe.stat("a\x00b") == (False, None, None)


def test_readable_matches_os_access(tmp_path):
    probe = OsFsProbe()
    f = tmp_path / "a.log"
    f.write_text("x", encoding="utf-8")
    assert probe.readable(str(f)) == os.access(str(f), os.R_OK)
    assert probe.readable(str(tmp_path / "nope")) is False
    assert probe.readable("a\x00b") is False


def test_listdir_returns_at_most_limit_names_and_never_opens(tmp_path, monkeypatch):
    for i in range(30):
        (tmp_path / f"f{i:02d}.log").write_text("secret", encoding="utf-8")
    real_open = builtins.open

    def guarded(file, *a, **k):
        if str(tmp_path) in str(file):
            raise AssertionError("a candidate file was opened")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded)
    probe = OsFsProbe()
    names, more = probe.listdir(str(tmp_path), 10)
    assert len(names) == 10 and more is True
    names, more = probe.listdir(str(tmp_path), 100)
    assert len(names) == 30 and more is False
    assert probe.listdir(str(tmp_path / "missing"), 5) == ([], False)
    assert probe.listdir(str(tmp_path / "f00.log"), 5) == ([], False)


def test_listdir_exact_limit_is_not_more(tmp_path):
    for i in range(5):
        (tmp_path / f"{i}.log").write_text("x", encoding="utf-8")
    assert OsFsProbe().listdir(str(tmp_path), 5)[1] is False


def test_linux_procfs_reads_only_comm_of_numeric_entries(tmp_path, monkeypatch):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("systemd\n", encoding="utf-8")
    (tmp_path / "1" / "cmdline").write_text("secret --token=abc", encoding="utf-8")
    (tmp_path / "20").mkdir()
    (tmp_path / "20" / "comm").write_text("nginx\n", encoding="utf-8")
    (tmp_path / "21").mkdir()                                  # no comm file: vanished process
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("x", encoding="utf-8")
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    procfs = LinuxProcFs(str(tmp_path))
    assert procfs.pids() == [1, 20, 21]
    assert procfs.comm(1) == "systemd" and procfs.comm(20) == "nginx"
    assert procfs.comm(21) is None
    assert all(p.replace("\\", "/").endswith("/comm") for p in opened) and opened


def test_linux_procfs_missing_root_is_empty():
    assert LinuxProcFs("/definitely/not/here").pids() == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real /proc")
def test_real_proc_has_own_pid():
    procfs = LinuxProcFs()
    assert os.getpid() in procfs.pids() and procfs.comm(os.getpid())
