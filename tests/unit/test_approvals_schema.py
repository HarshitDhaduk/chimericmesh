from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from chimericmesh.approvals.errors import ApprovalsNotMigrated
from chimericmesh.approvals.limits import MAX_PROPOSALS_PER_SERVICE_HOUR
from chimericmesh.approvals.rulebook import ensure_migrated
from chimericmesh.approvals.schema import APPROVALS_DDL, approvals_registry, register_migrations
from chimericmesh.store import Store
from chimericmesh.store.schema import core_registry

ROW = ("r1", 1, "svc", "signature", "disk full", 3, 100.0, 3, 1, 5, 100, "active", "", "me", 1.0, 1.0, None, "", 0,
       "/code")
INSERT = ("INSERT INTO approval_rules (id, seq, service_id, match_type, match_value, most_urgent_severity, "
          "expires_at, max_per_hour, max_per_incident, max_files, max_lines, status, status_reason, created_by, "
          "created_at, updated_at, source_proposal_id, note, use_count, code_folder) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def put(tx, **override):
    names = ["id", "seq", "service_id", "match_type", "match_value", "most_urgent_severity", "expires_at",
             "max_per_hour", "max_per_incident", "max_files", "max_lines", "status", "status_reason", "created_by",
             "created_at", "updated_at", "source_proposal_id", "note", "use_count", "code_folder"]
    values = dict(zip(names, ROW))
    values.update(override)
    tx.execute(INSERT, [values[n] for n in names])


@pytest.fixture(params=["file", "memory"])
def store(request, tmp_path: Path):
    path = str(tmp_path / "s.db") if request.param == "file" else ":memory:"
    s = Store.open(path, registry=approvals_registry())
    yield s
    s.close()


def test_tables_indexes_and_version_block(store):
    names = store.read_sync(lambda tx: {r[0] for r in tx.all("SELECT name FROM sqlite_master")}, allow_blocking=True)
    assert {"approval_rules", "rule_uses", "approval_state", "incident_openings", "idx_rules_service",
            "idx_uses_rule", "idx_uses_incident"} <= names
    versions = store.read_sync(lambda tx: [r[0] for r in tx.all("SELECT version FROM migrations "
                                                                  "WHERE module = 'approvals'")], allow_blocking=True)
    assert versions == [400] and all(400 <= v <= 499 for v in versions)


@pytest.mark.parametrize("override", [
    {"most_urgent_severity": 2}, {"max_per_hour": 11}, {"max_per_hour": 0}, {"match_value": "x" * 201},
    {"match_value": ""}, {"status": "weird"}, {"match_type": "regex"}, {"max_per_incident": 4}, {"max_files": 21},
    {"max_lines": 501}, {"note": "n" * 201},
])
def test_check_constraints_reject_bad_rows(store, override):
    with pytest.raises(Exception) as err:
        store.transact_sync(lambda tx: put(tx, **override), allow_blocking=True)
    assert isinstance(err.value, sqlite3.IntegrityError) or "CHECK" in str(err.value) or "constraint" in str(err.value).lower()


def test_valid_row_and_each_status_accepted(store):
    for n, status in enumerate(("active", "suspended", "revoked", "expired"), 1):
        store.transact_sync(lambda tx, n=n, status=status: put(tx, id=f"r{n}", seq=n, status=status),
                            allow_blocking=True)


def test_incident_openings_primary_key_and_no_reopen_column(store):
    cols = store.read_sync(lambda tx: [r[1] for r in tx.all("PRAGMA table_info(rule_uses)")], allow_blocking=True)
    assert "reopen_seen_at" not in cols and "completed_at" in cols
    store.transact_sync(lambda tx: tx.execute("INSERT INTO incident_openings VALUES ('i1', 2, 5.0)"),
                        allow_blocking=True)
    with pytest.raises(Exception):
        store.transact_sync(lambda tx: tx.execute("INSERT INTO incident_openings VALUES ('i1', 2, 9.0)"),
                            allow_blocking=True)


def test_ensure_migrated_raises_without_the_migration(tmp_path):
    s = Store.open(str(tmp_path / "plain.db"), registry=core_registry())
    try:
        with pytest.raises(ApprovalsNotMigrated):
            ensure_migrated(s)
    finally:
        s.close()


def test_ensure_migrated_passes_with_the_migration(store):
    ensure_migrated(store)


def test_ddl_max_per_hour_literal_equals_the_constant():
    ddl = "\n".join(APPROVALS_DDL)
    match = re.search(r"max_per_hour\s+INTEGER NOT NULL CHECK \(max_per_hour BETWEEN 1 AND (\d+)\)", ddl)
    assert match and int(match.group(1)) == MAX_PROPOSALS_PER_SERVICE_HOUR


def test_register_migrations_is_in_the_approvals_block():
    from chimericmesh.store.migrations import MigrationRegistry

    reg = MigrationRegistry()
    register_migrations(reg)
    assert [m.version for m in reg.migrations()] == [400]
