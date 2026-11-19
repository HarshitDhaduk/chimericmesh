"""Migration services:0100-registry (R1.1, R1.6, R1.7, R3.1)."""
from __future__ import annotations

import sqlite3

import pytest

from chimericmesh.services.schema import register_migrations, services_registry
from chimericmesh.store import MigrationError, MigrationRegistry, Store
from chimericmesh.store.schema import core_registry


def _db(store: Store, fn):
    return store.read_sync(fn, allow_blocking=True)


def _write(store: Store, fn):
    return store.transact_sync(fn, allow_blocking=True)


def _tables(store):
    return {r[0] for r in _db(store, lambda tx: tx.all("SELECT name FROM sqlite_master WHERE type='table'"))}


def test_tables_and_version(svc_store):
    t = _tables(svc_store)
    assert {"services", "log_sources", "service_tombstones"} <= t
    assert "service_stats" not in t
    assert _db(svc_store, lambda tx: tx.one("PRAGMA user_version")[0]) >= 100
    cols = {r[1] for r in _db(svc_store, lambda tx: tx.all("PRAGMA table_info(log_sources)"))}
    assert "path_key" not in cols and "backfill_lines" in cols
    idx = {r[0] for r in _db(svc_store, lambda tx: tx.all("SELECT name FROM sqlite_master WHERE type='index'"))}
    assert "idx_log_sources_service" in idx


def test_migration_applies_once(tmp_path):
    path = tmp_path / "x.db"
    for _ in range(2):
        s = Store.open(path, registry=services_registry())
        s.close()
    s = Store.open(path, registry=services_registry())
    try:
        rows = _db(s, lambda tx: tx.all("SELECT name FROM migrations WHERE name = 'services:0100-registry'"))
        assert len(rows) == 1
    finally:
        s.close()


def _ins_service(tx, sid="web", name="Web", folder=None, seq=1):
    tx.execute("INSERT INTO services (id, seq, name, code_folder, created_at, updated_at) VALUES (?,?,?,?,0,0)",
               (sid, seq, name, folder))


@pytest.mark.parametrize("kwargs", [
    {"sid": "unassigned"}, {"name": "n" * 81}, {"name": ""}, {"folder": "f" * 1025},
])
def test_service_checks(svc_store, kwargs):
    with pytest.raises(sqlite3.IntegrityError):
        _write(svc_store, lambda tx: _ins_service(tx, **kwargs))


def test_log_source_checks_and_cascade(svc_store):
    def ins(tx, sid, n, backfill=0):
        tx.execute("INSERT INTO log_sources (id, seq, service_id, path, format, backfill_lines, origin, created_at, updated_at)"
                   " VALUES (?,?,?,?,?,?,?,0,0)", (sid, n, "web", "/x", "plain", backfill, "user"))

    _write(svc_store, lambda tx: _ins_service(tx))
    for bad in (-1, 1001):
        with pytest.raises(sqlite3.IntegrityError):
            _write(svc_store, lambda tx, b=bad: ins(tx, "ls1", 1, b))
    _write(svc_store, lambda tx: ins(tx, "ls1", 1))
    _write(svc_store, lambda tx: tx.execute(
        "INSERT INTO events (ts, source, host, app, severity, message, service_id, ingested) VALUES (0,'s','h','a',3,'m','web',0)"))
    _write(svc_store, lambda tx: tx.execute(
        "INSERT INTO incidents (id, host, service_id, category, signature, severity, count, first_seen, last_seen, sample)"
        " VALUES ('i1','h','web','c','s',3,1,0,0,'x')"))
    _write(svc_store, lambda tx: tx.execute("DELETE FROM services WHERE id = 'web'"))
    assert _db(svc_store, lambda tx: tx.one("SELECT COUNT(*) FROM log_sources")[0]) == 0
    assert _db(svc_store, lambda tx: tx.one("SELECT COUNT(*) FROM events WHERE service_id='web'")[0]) == 1
    assert _db(svc_store, lambda tx: tx.one("SELECT COUNT(*) FROM incidents WHERE service_id='web'")[0]) == 1


def test_tombstones_are_unique(svc_store):
    _write(svc_store, lambda tx: tx.execute("INSERT INTO service_tombstones VALUES ('a', 0)"))
    with pytest.raises(sqlite3.IntegrityError):
        _write(svc_store, lambda tx: tx.execute("INSERT INTO service_tombstones VALUES ('a', 0)"))


def test_version_outside_block_is_rejected():
    reg = MigrationRegistry()
    with pytest.raises(MigrationError):
        reg.register("services", 99, "services:bad", ())
    with pytest.raises(MigrationError):
        reg.register("services", 200, "services:bad2", ())
    register_migrations(reg)  # the real one is inside the block


def test_core_registry_alone_has_no_services_tables(tmp_path):
    s = Store.open(tmp_path / "c.db", registry=core_registry())
    try:
        assert "services" not in _tables(s)
    finally:
        s.close()
