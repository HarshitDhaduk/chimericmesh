"""Task 6.1: migration connectors:0200-tail-offsets."""
from __future__ import annotations

import sqlite3

import pytest

from chimericmesh.connectors.schema import TAIL_OFFSETS_DDL, connectors_registry, register_migrations
from chimericmesh.services.schema import services_registry
from chimericmesh.store import MigrationError, Store
from chimericmesh.store.migrations import MODULE_BLOCKS

SHA = "a" * 64


def _read(store, fn):
    return store.read_sync(fn, allow_blocking=True)


def _write(store, fn):
    return store.transact_sync(fn, allow_blocking=True)


def _tables(store) -> set[str]:
    return {r[0] for r in _read(store, lambda tx: tx.all("SELECT name FROM sqlite_master WHERE type='table'"))}


def _insert(store, sid="src-1", dev="1", ino="2", head_len=10, head_sha=SHA, offset=0, discarding=0):
    _write(store, lambda tx: tx.execute(
        "INSERT INTO tail_offsets (log_source_id, dev, ino, head_len, head_sha, byte_offset, discarding,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (sid, dev, ino, head_len, head_sha, offset, discarding)))


@pytest.fixture
def cstore(tmp_path):
    store = Store.open(tmp_path / "c.db", registry=connectors_registry())
    yield store
    store.close()


def test_table_exists_and_user_version(cstore):
    assert "tail_offsets" in _tables(cstore)
    assert _read(cstore, lambda tx: tx.one("PRAGMA user_version")[0]) >= 200


def test_applied_once(tmp_path):
    path = tmp_path / "x.db"
    for _ in range(2):
        Store.open(path, registry=connectors_registry()).close()
    s = Store.open(path, registry=connectors_registry())
    try:
        rows = _read(s, lambda tx: tx.all("SELECT name FROM migrations WHERE name = 'connectors:0200-tail-offsets'"))
        assert len(rows) == 1
    finally:
        s.close()


def test_checks(cstore):
    _insert(cstore)
    for kw in ({"sid": "s2", "head_sha": "a" * 10}, {"sid": "s3", "head_len": 257}, {"sid": "s4", "offset": -1},
               {"sid": "x" * 31}, {"sid": ""}, {"sid": "s5", "discarding": 2}):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(cstore, **kw)


def test_huge_dev_and_ino_round_trip_as_text(cstore):
    big = str(2**64 - 1)
    _insert(cstore, dev=big, ino=big)
    row = _read(cstore, lambda tx: tx.one("SELECT dev, ino FROM tail_offsets"))
    assert (row[0], row[1]) == (big, big) and int(row[0]) == 2**64 - 1


def test_services_registry_alone_has_no_table(tmp_path):
    s = Store.open(tmp_path / "s.db", registry=services_registry())
    try:
        assert "tail_offsets" not in _tables(s)
    finally:
        s.close()


def test_version_block():
    low, high = MODULE_BLOCKS["connectors"]
    assert low <= 200 <= high
    reg = services_registry()
    register_migrations(reg)
    assert any(m.name == "connectors:0200-tail-offsets" and m.version == 200 for m in reg.migrations())
    with pytest.raises(MigrationError):
        services_registry().register("connectors", 100, "connectors:0100-x", TAIL_OFFSETS_DDL)


def test_no_foreign_key(cstore):
    assert _read(cstore, lambda tx: tx.all("PRAGMA foreign_key_list(tail_offsets)")) == []
