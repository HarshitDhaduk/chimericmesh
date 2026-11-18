import re
import sqlite3
from pathlib import Path

import pytest

from chimericmesh.store import schema as schema_mod
from chimericmesh.store.errors import MigrationError
from chimericmesh.store.migrations import MigrationRegistry, apply_migrations
from chimericmesh.store.schema import core_registry, register_core_migrations


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def names(c, kind="table"):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def uv(c) -> int:
    return c.execute("PRAGMA user_version").fetchone()[0]


def test_new_database_gets_core_schema():
    c = fresh()
    apply_migrations(c, core_registry())
    assert {"events", "incidents", "proposals", "audit_log", "counters", "migrations"} <= names(c)
    assert {
        "idx_events_ingested", "idx_events_service", "idx_incidents_service", "idx_incidents_last",
        "idx_proposals_status", "idx_proposals_incident", "idx_proposals_service",
        "idx_audit_service", "idx_audit_rule", "idx_audit_proposal",
    } <= names(c, "index")
    assert {"audit_no_update", "audit_no_delete"} <= names(c, "trigger")
    assert uv(c) == 1
    assert c.execute("SELECT version, module, name FROM migrations").fetchall() == [
        (1, "store", "store:0001-core-tables")
    ]
    cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
    assert "ingested" in cols


def test_m1_bootstrap_is_the_only_definition_of_migrations_table():
    c = fresh()
    apply_migrations(c, core_registry())  # must not fail with "table migrations already exists"
    ddl = " ".join(schema_mod.CORE_TABLES_DDL).lower()
    assert "create table migrations" not in ddl and "table if not exists migrations" not in ddl


def test_reopen_applies_nothing():
    c = fresh()
    reg = core_registry()
    apply_migrations(c, reg)
    before = c.execute("SELECT version, applied_at FROM migrations").fetchall()
    apply_migrations(c, reg)
    assert c.execute("SELECT version, applied_at FROM migrations").fetchall() == before


def test_extra_migration_runs_exactly_once():
    c = fresh()
    reg = core_registry()
    reg.register("services", 100, "services:0100", ("CREATE TABLE svc (id TEXT)", "INSERT INTO svc VALUES ('a')"))
    apply_migrations(c, reg)
    apply_migrations(c, reg)
    assert c.execute("SELECT COUNT(*) FROM svc").fetchone()[0] == 1
    assert uv(c) == 100


def test_later_lower_version_still_runs_once_and_user_version_keeps_max():
    c = fresh()
    reg = core_registry()
    reg.register("connectors", 250, "connectors:0250", ("CREATE TABLE late (x)",))
    apply_migrations(c, reg)
    assert uv(c) == 250
    reg.register("services", 120, "services:0120", ("CREATE TABLE mid (x)", "INSERT INTO mid VALUES (1)"))
    apply_migrations(c, reg)
    apply_migrations(c, reg)
    assert c.execute("SELECT COUNT(*) FROM mid").fetchone()[0] == 1
    assert uv(c) == 250


def test_failing_migration_rolls_back_everything_and_names_itself():
    c = fresh()
    reg = core_registry()
    apply_migrations(c, reg)
    reg.register("services", 100, "services:0100-broken", ("CREATE TABLE half (x)", "THIS IS NOT SQL"))
    with pytest.raises(MigrationError) as ei:
        apply_migrations(c, reg)
    assert "services:0100-broken" in str(ei.value)
    assert "half" not in names(c)
    assert uv(c) == 1
    assert c.execute("SELECT COUNT(*) FROM migrations WHERE version=100").fetchone()[0] == 0
    assert not c.in_transaction


def test_callable_migration_inside_open_transaction():
    c = fresh()
    reg = core_registry()
    seen = []

    def mig(conn):
        seen.append(conn.in_transaction)
        conn.execute("CREATE TABLE viacall (x)")

    reg.register("services", 100, "services:0100-call", mig)
    apply_migrations(c, reg)
    assert seen == [True] and "viacall" in names(c) and uv(c) == 100


def test_callable_that_commits_is_rejected():
    c = fresh()
    reg = core_registry()

    def bad(conn):
        conn.execute("CREATE TABLE sneaky (x)")
        conn.execute("COMMIT")

    reg.register("services", 100, "services:0100-commits", bad)
    with pytest.raises(MigrationError) as ei:
        apply_migrations(c, reg)
    msg = str(ei.value)
    assert "services:0100-commits" in msg and "cannot be undone" in msg
    assert c.execute("SELECT COUNT(*) FROM migrations WHERE version=100").fetchone()[0] == 0
    assert uv(c) == 1


def test_events_ingested_column_is_required():
    c = fresh()
    apply_migrations(c, core_registry())
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO events (id, ts, source, host, app, severity, message) VALUES (1,1,'s','h','a',3,'m')")


def test_incident_column_checks():
    c = fresh()
    apply_migrations(c, core_registry())
    ins = (
        "INSERT INTO incidents (id, host, category, signature, severity, count, first_seen, last_seen, sample)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
    )
    c.execute(ins, ("a", "h" * 255, "c", "s", 3, 1, 1.0, 1.0, "x" * 4096))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("b", "h" * 256, "c", "s", 3, 1, 1.0, 1.0, "x"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("c", "h", "c", "s", 3, 1, 1.0, 1.0, "x" * 4097))
    with pytest.raises(sqlite3.IntegrityError):  # resolved without resolved_at
        c.execute("UPDATE incidents SET resolved = 1 WHERE id = 'a'")


def test_proposal_status_check_and_seq_unique():
    c = fresh()
    apply_migrations(c, core_registry())
    ins = "INSERT INTO proposals (id, seq, incident_id, action, status, created) VALUES (?,?,?,?,?,?)"
    c.execute(ins, ("p1", 1, "i", "a", "pending", 1.0))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("p2", 2, "i", "a", "weird", 1.0))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("p3", 1, "i", "a", "pending", 1.0))


def test_audit_triggers_block_update_and_delete():
    c = fresh()
    apply_migrations(c, core_registry())
    c.execute("INSERT INTO audit_log (ts, actor, action, subject) VALUES (1, 'a', 'b', 'c')")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit_log SET actor = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit_log")


def test_register_core_migrations_registers_store_block_one():
    reg = MigrationRegistry()
    register_core_migrations(reg)
    assert [m.name for m in reg.migrations()] == ["store:0001-core-tables"]


def test_user_version_helper_is_the_only_dynamic_sql():
    src = Path(__import__("chimericmesh.store.migrations", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert len(re.findall(r'f"[^"]*(PRAGMA|SELECT|INSERT|CREATE)', src)) <= 1
    from chimericmesh.store.migrations import _set_user_version

    c = fresh()
    _set_user_version(c, 5)
    assert uv(c) == 5
    with pytest.raises((ValueError, TypeError)):
        _set_user_version(c, "5; DROP TABLE x")  # type: ignore[arg-type]
