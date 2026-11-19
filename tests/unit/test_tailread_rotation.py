"""Task 4.2: rotation, truncation, special files, missing and unreadable (sub-criteria 3.3a to 3.3g)."""
from __future__ import annotations

import errno
import os
import stat
import sys
import types

import pytest

from chimericmesh.connectors.tailread import FileIdent, TailState, poll_file

WIN = sys.platform == "win32"


def append(path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def datas(result) -> list[bytes]:
    return [x.data for x in result.lines]


def attach(path):
    return poll_file(str(path), TailState())


def busy_error() -> OSError:
    if WIN:
        return PermissionError(13, "The process cannot access the file because it is being used", None, 32)
    return OSError(errno.EBUSY, "Device or resource busy")


def test_3_3a_no_handle_between_polls(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    for i in range(20):
        append(p, b"line %d\n" % i)
        r = poll_file(str(p), s)
        assert datas(r) == [b"line %d" % i]
        s = r.state
        moved = str(p) + ".1"
        os.rename(p, moved)
        os.rename(moved, p)


def test_3_3b_rename_and_recreate_reads_new_file_from_start(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"old1\nold2\n")
    s = attach(p).state
    append(p, b"old3\n")
    s = poll_file(str(p), s).state
    os.rename(p, str(p) + ".1")
    p.write_bytes(b"new1\nnew2\n")
    r = poll_file(str(p), s)
    assert r.reason == "rotated" and datas(r) == [b"new1", b"new2"]


def test_3_3b_delete_and_create_reads_new_file_from_start(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"old1\nold2\n")
    s = attach(p).state
    os.remove(p)
    p.write_bytes(b"brand new content\nsecond\n")
    r = poll_file(str(p), s)
    assert r.reason == "rotated" and datas(r) == [b"brand new content", b"second"]


def test_3_3c_copy_truncate_reads_from_start(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"one\ntwo\nthree\n")
    s = attach(p).state
    append(p, b"four\n")
    s = poll_file(str(p), s).state
    with open(p, "r+b") as f:
        f.truncate(0)
    append(p, b"x\n")
    r = poll_file(str(p), s)
    assert r.reason == "truncated" and datas(r) == [b"x"]


def test_3_3d_and_3_3e_no_line_twice_across_rotations(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"")
    s = attach(p).state
    got: list[bytes] = []
    n = 0
    for polls in range(10):
        append(p, b"L%d\n" % n)
        n += 1
        r = poll_file(str(p), s)
        got += datas(r)
        s = r.state
        if polls in (2, 5, 8):
            os.rename(p, str(p) + f".{polls}")
            p.write_bytes(b"")
    assert got == [b"L%d" % i for i in range(10)]


def test_3_3f_lines_after_last_poll_and_before_rename_are_lost(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"a\n")
    s = attach(p).state
    append(p, b"polled\n")
    r = poll_file(str(p), s)
    assert datas(r) == [b"polled"]
    append(p, b"unread\n")
    os.rename(p, str(p) + ".1")
    p.write_bytes(b"fresh\n")
    r2 = poll_file(str(p), r.state)
    assert datas(r2) == [b"fresh"]


def test_3_3f_unread_backlog_of_a_more_poll_is_lost_too(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"a\n")
    s = attach(p).state
    append(p, b"".join(b"line %04d %s\n" % (i, b"p" * 40) for i in range(200)))
    r = poll_file(str(p), s, max_read=2048)
    assert r.more and 0 < len(r.lines) < 200
    os.rename(p, str(p) + ".1")
    p.write_bytes(b"fresh\n")
    r2 = poll_file(str(p), r.state, max_read=2048)
    assert datas(r2) == [b"fresh"]


def test_3_3g_copy_truncate_and_regrow_past_old_offset(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"aaaa\nbbbb\n")
    s = attach(p).state
    append(p, b"cccc\n")
    s = poll_file(str(p), s).state
    with open(p, "r+b") as f:
        f.truncate(0)
    p.write_bytes(b"zzzz1\nzzzz2\nzzzz3\nzzzz4\n")
    assert p.stat().st_size > s.offset
    r = poll_file(str(p), s)
    assert r.reason == "rotated"
    assert datas(r) == [b"zzzz1", b"zzzz2", b"zzzz3", b"zzzz4"]


def test_missing_keeps_state_and_new_identity_reads_from_start(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"one\n")
    s = attach(p).state
    os.remove(p)
    r = poll_file(str(p), s)
    assert r.status == "missing" and r.state.ident == s.ident and r.state.offset == s.offset
    p.write_bytes(b"completely different\nfile\n")
    r2 = poll_file(str(p), r.state)
    assert r2.status == "running" and r2.reason == "rotated"
    assert datas(r2) == [b"completely different", b"file"]


def test_missing_at_first_attach_is_read_from_start_when_it_appears(tmp_path):
    p = tmp_path / "later.log"
    r = poll_file(str(p), TailState())
    assert r.status == "missing" and r.state.ident is None and r.state.attach == "begin"
    p.write_bytes(b"first\nsecond\n")
    r2 = poll_file(str(p), r.state)
    assert r2.status == "running" and r2.reason == "appeared" and datas(r2) == [b"first", b"second"]


def test_permission_error_is_unreadable_with_os_text(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"y\n")

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(os, "open", deny)
    r = poll_file(str(p), s)
    assert r.status == "unreadable" and "Permission denied" in r.error and r.lines == ()
    assert r.state == s


def test_permission_error_on_stat_is_unreadable(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")

    def deny(*a, **k):
        raise PermissionError(13, "Access is denied", str(p))

    monkeypatch.setattr(os, "stat", deny)
    r = poll_file(str(p), TailState())
    assert r.status == "unreadable" and "denied" in r.error.lower()


def test_sharing_violation_is_busy_and_next_poll_succeeds(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"y\n")
    real = os.open

    def locked(*a, **k):
        raise busy_error()

    monkeypatch.setattr(os, "open", locked)
    r = poll_file(str(p), s)
    assert r.status == "busy" and r.lines == () and r.state == s
    monkeypatch.setattr(os, "open", real)
    r2 = poll_file(str(p), s)
    assert r2.status == "running" and datas(r2) == [b"y"]


def test_invalid_path_is_error(tmp_path, monkeypatch):
    r = poll_file("nul\x00name", TailState())
    assert r.status == "error" and r.error == "invalid path"

    def unencodable(*a, **k):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(os, "open", unencodable)
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    r = poll_file(str(p), TailState())
    assert r.status == "error" and r.error == "invalid path"


def test_directory_is_not_a_regular_file(tmp_path):
    r = poll_file(str(tmp_path), TailState())
    assert r.status == "error" and r.error == "not a regular file"


@pytest.mark.skipif(WIN, reason="FIFOs are POSIX only")
def test_fifo_is_not_opened(tmp_path):
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    r = poll_file(str(fifo), TailState())
    assert r.status == "error" and r.error == "not a regular file"


def test_symlink_to_regular_file_is_followed(tmp_path):
    target = tmp_path / "real.log"
    target.write_bytes(b"x\n")
    link = tmp_path / "link.log"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    s = attach(link).state
    append(target, b"via link\n")
    assert datas(poll_file(str(link), s)) == [b"via link"]


def test_file_type_is_rechecked_after_open(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"y\n")
    real = os.fstat

    def fake(fd):
        st = real(fd)
        return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=st.st_dev, st_ino=st.st_ino,
                                     st_size=st.st_size, st_mtime_ns=st.st_mtime_ns)

    monkeypatch.setattr(os, "fstat", fake)
    r = poll_file(str(p), s)
    assert r.status == "error" and r.error == "not a regular file"


def test_inode_zero_falls_back_to_the_fingerprint(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"head line\nsecond\n")
    s = attach(p).state
    zero = TailState(ident=FileIdent(s.ident.dev, 0, s.ident.head_len, s.ident.head_sha), offset=s.offset)
    append(p, b"more\n")
    r = poll_file(str(p), zero)
    assert datas(r) == [b"more"] and r.reason == "resumed"
    other = TailState(ident=FileIdent(s.ident.dev, 0, 5, "0" * 64), offset=s.offset)
    r2 = poll_file(str(p), other)
    assert r2.reason == "rotated" and datas(r2)[0] == b"head line"


def test_huge_inode_and_device_do_not_fail(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    big = 2**64 - 1
    real_stat, real_fstat = os.stat, os.fstat

    def wrap(real):
        def inner(*a, **k):
            st = real(*a, **k)
            return types.SimpleNamespace(st_mode=st.st_mode, st_dev=big, st_ino=big - 1, st_size=st.st_size,
                                         st_mtime_ns=st.st_mtime_ns)
        return inner

    monkeypatch.setattr(os, "stat", wrap(real_stat))
    monkeypatch.setattr(os, "fstat", wrap(real_fstat))
    r = poll_file(str(p), TailState())
    assert r.status == "running" and r.state.ident.dev == big and r.state.ident.ino == big - 1
    append(p, b"y\n")
    assert datas(poll_file(str(p), r.state)) == [b"y"]
