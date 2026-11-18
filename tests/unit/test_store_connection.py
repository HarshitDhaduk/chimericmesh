import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from chimericmesh.store import connection as conn_mod
from chimericmesh.store.connection import open_connection
from chimericmesh.store.errors import SchemaTooNewError, StoreCorruptError, StoreOpenError
from chimericmesh.store.migrations import MigrationRegistry


def registry(highest: int = 1) -> MigrationRegistry:
    reg = MigrationRegistry()
    if highest:
        reg.register("store", highest, f"store:{highest:04d}", ())
    return reg


def listing(d: Path) -> list[tuple[str, int]]:
    return sorted((p.name, p.stat().st_size) for p in d.iterdir())


def make_wal_db(path: Path, user_version: int) -> None:
    c = sqlite3.connect(path, isolation_level=None)
    assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    c.execute("CREATE TABLE t (x)")
    c.execute(f"PRAGMA user_version={user_version}")
    c.close()


def test_non_sqlite_file_is_refused_and_untouched(tmp_path):
    p = tmp_path / "bad.db"
    p.write_bytes(b"this is definitely not a sqlite database file, " * 10)
    before_bytes, before_dir = p.read_bytes(), listing(tmp_path)
    with pytest.raises(StoreCorruptError) as ei:
        open_connection(str(p), registry())
    assert str(p) in str(ei.value)
    assert p.read_bytes() == before_bytes
    assert listing(tmp_path) == before_dir


def test_short_nonempty_file_is_corrupt(tmp_path):
    p = tmp_path / "short.db"
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 10)
    with pytest.raises(StoreCorruptError):
        open_connection(str(p), registry())
    assert p.stat().st_size == 26


def test_zero_length_file_opens(tmp_path):
    p = tmp_path / "empty.db"
    p.write_bytes(b"")
    c = open_connection(str(p), registry())
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        c.close()


def test_too_new_graceful_wal_database_is_refused_without_any_change(tmp_path):
    p = tmp_path / "new.db"
    make_wal_db(p, 7)
    assert listing(tmp_path) == [("new.db", p.stat().st_size)]  # graceful close removed -wal/-shm
    before_bytes, before_dir = p.read_bytes(), listing(tmp_path)
    with pytest.raises(SchemaTooNewError) as ei:
        open_connection(str(p), registry(1))
    assert (ei.value.found, ei.value.known) == (7, 1)
    assert p.read_bytes() == before_bytes
    assert listing(tmp_path) == before_dir  # no -wal / -shm appeared


def test_old_sqlite_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 23, 1))
    with pytest.raises(StoreOpenError) as ei:
        open_connection(str(tmp_path / "x.db"), registry())
    assert "3.23" in str(ei.value)
    assert not (tmp_path / "x.db").exists()


def test_pragmas(tmp_path):
    c = open_connection(str(tmp_path / "p.db"), registry())
    try:
        one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        assert one("PRAGMA journal_mode") == "wal"
        assert one("PRAGMA busy_timeout") == 5000
        assert one("PRAGMA synchronous") == 1
        assert one("PRAGMA foreign_keys") == 1
        assert one("PRAGMA wal_autocheckpoint") == 1000
        assert c.isolation_level is None
    finally:
        c.close()


def test_connection_usable_from_another_thread(tmp_path):
    import threading

    c = open_connection(str(tmp_path / "t.db"), registry())
    out = []
    t = threading.Thread(target=lambda: out.append(c.execute("SELECT 1").fetchone()[0]))
    t.start()
    t.join()
    c.close()
    assert out == [1]


def test_parent_directory_is_created_and_secured_only_when_new(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(conn_mod.fsperms, "secure_dir", lambda p, **kw: calls.append(("dir", Path(p))) or True)
    monkeypatch.setattr(conn_mod.fsperms, "secure_file", lambda p, **kw: calls.append(("file", Path(p))) or True)
    home = tmp_path / "newhome"
    db = home / "a.db"
    open_connection(str(db), registry()).close()
    assert db.exists()
    assert ("dir", home) in calls and ("file", db) in calls
    calls.clear()
    open_connection(str(db), registry()).close()  # second open: nothing is created, nothing re-secured
    assert [c for c in calls if c[0] == "dir" or c[1] == db] == []  # only -wal/-shm are re-secured


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_new_file_mode_is_0600(tmp_path):
    db = tmp_path / "home" / "m.db"
    open_connection(str(db), registry()).close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_memory_database_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = open_connection(":memory:", registry())
    c.execute("CREATE TABLE x (a)")
    c.close()
    assert list(tmp_path.iterdir()) == []


def test_unwritable_parent_raises_store_open_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(StoreOpenError):
        open_connection(str(blocker / "sub" / "a.db"), registry())


def test_source_uses_no_readonly_or_immutable_uri():
    src = Path(conn_mod.__file__).read_text(encoding="utf-8")
    assert "immutable=1" not in src and "mode=ro" not in src
