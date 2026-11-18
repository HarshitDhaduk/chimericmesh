"""Second open-time guard: a crash can leave a new user_version only in the -wal file (review M2)."""
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from chimericmesh.store import connection as conn_mod
from chimericmesh.store.connection import open_connection
from chimericmesh.store.errors import SchemaTooNewError
from chimericmesh.store.migrations import MigrationRegistry

HIGH = 7

CRASH_SCRIPT = f"""
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, message TEXT)")
c.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, message TEXT)")
c.execute("CREATE TABLE proposals (id INTEGER PRIMARY KEY, message TEXT)")
c.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER)")
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("BEGIN")
c.execute("INSERT INTO events VALUES (1, 'e')")
c.execute("INSERT INTO incidents VALUES (1, 'i')")
c.execute("INSERT INTO proposals VALUES (1, 'p')")
c.execute("PRAGMA user_version={HIGH}")
c.execute("COMMIT")
os._exit(0)
"""


def registry(highest: int = 1) -> MigrationRegistry:
    reg = MigrationRegistry()
    reg.register("store", highest, f"store:{highest:04d}", ())
    return reg


def make_crashed_db(path: Path) -> None:
    subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(path)], check=True, timeout=60)


def raw_user_version_in_header(path: Path) -> int:
    return struct.unpack(">i", path.read_bytes()[60:64])[0]


def test_fixture_reproduces_the_stale_header(tmp_path):
    db = tmp_path / "crash.db"
    make_crashed_db(db)
    wal = tmp_path / "crash.db-wal"
    assert wal.exists() and wal.stat().st_size > 0
    assert raw_user_version_in_header(db) == 0  # the byte guard cannot see the new value


def test_second_guard_refuses_a_crashed_too_new_database_and_writes_nothing(tmp_path):
    db = tmp_path / "crash.db"
    make_crashed_db(db)
    with pytest.raises(SchemaTooNewError) as ei:
        open_connection(str(db), registry(1))
    assert ei.value.found == HIGH
    check = sqlite3.connect(db)
    try:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "migrations" not in tables
        assert check.execute("SELECT COUNT(*) FROM counters WHERE name='clean_shutdown'").fetchone()[0] == 0
        assert check.execute("PRAGMA user_version").fetchone()[0] == HIGH
        # O1: rows committed before the crash are all still there (the files may change, the data not)
        for table in ("events", "incidents", "proposals"):
            assert check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        check.close()


def test_byte_guard_runs_first_so_no_connection_is_needed(tmp_path, monkeypatch):
    db = tmp_path / "new.db"
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.execute(f"PRAGMA user_version={HIGH}")
    c.close()

    def no_connect(*a, **k):
        raise AssertionError("sqlite3.connect must not be called by the byte guard")

    monkeypatch.setattr(sqlite3, "connect", no_connect)
    with pytest.raises(SchemaTooNewError):
        open_connection(str(db), registry(1))


def test_no_readonly_or_immutable_uri_in_source():
    src = Path(conn_mod.__file__).read_text(encoding="utf-8")
    assert "immutable=1" not in src and "mode=ro" not in src and "file:" not in src
