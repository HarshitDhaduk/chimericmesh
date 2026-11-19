"""Task 4.4: NTFS identity and rename behaviour, measured with real files (design Overview table)."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from chimericmesh.connectors.tailread import TailState, poll_file

WIN = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WIN, reason="NTFS specific")
# A rename that lands inside a poll fails while the poll holds the handle. On this machine a changed-file poll
# usually holds it about 2.5 ms, but on-access antivirus scanning of the open, or a loaded machine, can stretch
# one poll to hundreds of milliseconds (measured 2026-11-19: every failure fell inside a poll). So the rotator
# waits for the poll in progress to return before each retry (at most 3 attempts) instead of sleeping 1 ms,
# and the test asserts what the design claims: every failure lands inside a poll and clears after it.
RETRIES = 3


def datas(result) -> list[bytes]:
    return [x.data for x in result.lines]


def test_renamed_file_keeps_its_inode_and_new_file_differs(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"one\n")
    before = os.stat(a)
    os.rename(a, tmp_path / "a.log.1")
    assert os.stat(tmp_path / "a.log.1").st_ino == before.st_ino
    a.write_bytes(b"two\n")
    assert os.stat(a).st_ino != before.st_ino


@windows_only
def test_delete_and_create_gives_a_different_inode(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"one\n")
    ino = os.stat(a).st_ino
    os.remove(a)
    a.write_bytes(b"one\n")
    assert os.stat(a).st_ino != ino


def test_copy_truncate_keeps_inode_and_shrinks_size(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x" * 100 + b"\n")
    before = os.stat(a)
    with open(a, "r+b") as f:
        f.truncate(0)
    after = os.stat(a)
    assert after.st_ino == before.st_ino and after.st_size == 0 < before.st_size


def test_fstat_and_stat_agree_on_device_and_inode(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x\n")
    fd = os.open(a, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        by_fd = os.fstat(fd)
    finally:
        os.close(fd)
    by_path = os.stat(a)
    assert (by_fd.st_dev, by_fd.st_ino) == (by_path.st_dev, by_path.st_ino)


@windows_only
def test_rename_fails_while_a_handle_is_open_and_works_after_poll(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x\n")
    with open(a, "rb"):
        with pytest.raises(PermissionError) as info:
            os.rename(a, tmp_path / "a.log.1")
        assert info.value.winerror == 32
    poll_file(str(a), TailState())
    os.rename(a, tmp_path / "a.log.1")  # no handle left behind by poll_file


def test_rename_succeeds_after_every_poll_on_any_platform(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x\n")
    s = poll_file(str(a), TailState()).state
    with open(a, "ab") as f:
        f.write(b"y\n")
    poll_file(str(a), s)
    os.rename(a, tmp_path / "a.log.1")


def test_iis_style_rotation_reads_the_new_file_from_its_start(tmp_path):
    log = tmp_path / "u_ex.log"
    log.write_bytes(b"#Software: IIS\n#Fields: date time cs-method\n2026-11-18 10:00:00 GET\n")
    s = poll_file(str(log), TailState()).state
    with open(log, "ab") as f:
        f.write(b"2026-11-18 10:00:01 GET\n")
    r = poll_file(str(log), s)
    assert datas(r) == [b"2026-11-18 10:00:01 GET"]
    os.rename(log, tmp_path / "u_ex.log.1")
    log.write_bytes(b"#Software: IIS\n#Fields: date time cs-method cs-uri-stem\n2026-11-19 00:00:00 GET /a\n")
    r2 = poll_file(str(log), r.state)
    assert r2.reason == "rotated"
    assert datas(r2)[0] == b"#Software: IIS" and datas(r2)[-1] == b"2026-11-19 00:00:00 GET /a"


def _append_shared(path, data: bytes) -> None:
    """Append the way IIS-like writers do: the handle allows delete and rename, so only polls can block a rename."""
    if not WIN:
        with open(path, "ab") as f:
            f.write(data)
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x0004, 7, None, 3, 0x80, None)  # FILE_APPEND_DATA, share all, OPEN_EXISTING
    if handle in (None, wintypes.HANDLE(-1).value):
        raise FileNotFoundError(path)
    try:
        written = wintypes.DWORD(0)
        k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    finally:
        k32.CloseHandle(handle)


def _wait_poll_done(in_poll: threading.Event, done: threading.Event) -> None:
    deadline = time.monotonic() + 10
    while in_poll.is_set() and time.monotonic() < deadline:
        done.wait(0.05)


def _rename_race(path, stop: threading.Event, stats: dict, in_poll: threading.Event,
                 done: threading.Event) -> None:
    away = str(path) + ".r"
    while not stop.is_set():
        for src, dst in ((str(path), away), (away, str(path))):
            for attempt in range(RETRIES):
                try:
                    os.rename(src, dst)
                    break
                except PermissionError:
                    stats["fails"] += 1
                    stats["inside_poll"] += in_poll.is_set()
                    _wait_poll_done(in_poll, done)
                except OSError:
                    break
            else:
                stats["hard"] += 1
            stats["renames"] += 1
            time.sleep(0.001)
    if os.path.exists(away) and not os.path.exists(path):
        os.rename(away, path)


def _run_race(path, state, append: bool, seconds: float) -> dict:
    stats = {"fails": 0, "hard": 0, "inside_poll": 0, "renames": 0, "polls": 0}
    stop = threading.Event()
    in_poll = threading.Event()
    done = threading.Event()
    t = threading.Thread(target=_rename_race, args=(path, stop, stats, in_poll, done))
    t.start()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            if append:
                try:
                    _append_shared(path, b"appended line\n")
                except OSError:
                    pass
                time.sleep(0.04)
            done.clear()
            in_poll.set()
            try:
                result = poll_file(str(path), state)
            finally:
                in_poll.clear()
                done.set()
            assert result.status in ("running", "missing", "busy", "unreadable")
            state = result.state
            stats["polls"] += 1
    finally:
        stop.set()
        t.join()
    return stats


def test_rename_race_idle_file_has_no_rename_failures(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x\n")
    s = poll_file(str(a), TailState()).state
    stats = _run_race(a, s, append=False, seconds=1.0)
    print("idle race:", stats)
    assert stats["renames"] > 20
    assert stats["fails"] == 0 and stats["hard"] == 0


def test_rename_race_while_appending_every_failure_clears_on_retry(tmp_path):
    a = tmp_path / "a.log"
    a.write_bytes(b"x\n")
    s = poll_file(str(a), TailState()).state
    stats = _run_race(a, s, append=True, seconds=1.0)
    rate = stats["fails"] / max(1, stats["renames"])
    print(f"appending race: {stats} failure rate {rate:.3%}")
    assert stats["renames"] > 20
    assert stats["hard"] == 0
    assert stats["inside_poll"] == stats["fails"]
