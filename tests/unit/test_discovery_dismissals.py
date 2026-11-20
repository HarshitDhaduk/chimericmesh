"""The dismissal table and its operations (R5.4, R5.7)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from chimericmesh.discovery import dismissals
from chimericmesh.discovery.schema import discovery_registry, register_migrations
from chimericmesh.store import MigrationRegistry, Store
from chimericmesh.store.config import MEMORY
from chimericmesh.store.migrations import MODULE_BLOCKS


def open_store(path=MEMORY):
    return Store.open(path, registry=discovery_registry())


def user_version(store) -> int:
    return store.read_sync(lambda tx: tx.one("PRAGMA user_version")[0], allow_blocking=True)


def count_rows(store) -> int:
    return store.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM discovery_dismissals")[0], allow_blocking=True)


def audit_actions(store) -> list[str]:
    rows = asyncio.run(store.audit.read(500))
    return [r["action"] for r in rows]


def test_migration_runs_once_and_registers_in_the_discovery_block():
    store = open_store()
    try:
        assert user_version(store) >= 300
        names = store.read_sync(lambda tx: [r["name"] for r in tx.all("SELECT name FROM migrations")], allow_blocking=True)
        assert names.count("discovery:0300-dismissals") == 1
    finally:
        store.close()
    assert 300 in range(*(lambda b: (b[0], b[1] + 1))(MODULE_BLOCKS["discovery"]))
    reg = MigrationRegistry()
    register_migrations(reg)
    assert [m.version for m in reg.migrations()] == [300]


def test_dismiss_is_idempotent_and_audits_once():
    store = open_store()
    try:
        assert asyncio.run(dismissals.dismiss(store, "nginx", "a" * 16, actor="rest:x", paths=3)) is True
        assert asyncio.run(dismissals.dismiss(store, "nginx", "a" * 16, actor="rest:x", paths=3)) is False
        assert count_rows(store) == 1
        assert audit_actions(store).count("discovery_dismissed") == 1
    finally:
        store.close()


def test_a_failing_audit_append_rolls_the_row_back(monkeypatch):
    store = open_store()
    try:
        def boom(*a, **k):
            raise RuntimeError("audit down")
        monkeypatch.setattr(store.audit, "append_tx", boom)
        with pytest.raises(RuntimeError):
            asyncio.run(dismissals.dismiss(store, "nginx", "b" * 16, actor="x", paths=1))
        assert count_rows(store) == 0
    finally:
        store.close()


def test_the_table_never_holds_more_than_500_rows_and_the_oldest_goes_first():
    store = open_store()
    try:
        async def fill():
            for i in range(505):
                await dismissals.dismiss(store, f"app{i}", f"{i:016d}", actor="x", paths=1)
        asyncio.run(fill())
        assert count_rows(store) == 500
        kept = asyncio.run(dismissals.load_fingerprints(store))
        assert ("app0", f"{0:016d}") not in kept and ("app504", f"{504:016d}") in kept
    finally:
        store.close()


def test_load_fingerprints_returns_a_frozenset_of_pairs():
    store = open_store()
    try:
        asyncio.run(dismissals.dismiss(store, "nginx", "c" * 16, actor="x", paths=1))
        got = asyncio.run(dismissals.load_fingerprints(store))
        assert isinstance(got, frozenset) and got == frozenset({("nginx", "c" * 16)})
        rows = asyncio.run(dismissals.list_dismissals(store))
        assert [r["app_id"] for r in rows] == ["nginx"] and isinstance(rows[0]["dismissed_at"], float)
        assert set(rows[0]) == {"app_id", "dismissed_at"}
    finally:
        store.close()


def test_clear_deletes_all_audits_the_count_and_returns_it():
    store = open_store()
    try:
        async def go():
            await dismissals.dismiss(store, "a", "1" * 16, actor="x", paths=1)
            await dismissals.dismiss(store, "b", "2" * 16, actor="x", paths=1)
            return await dismissals.clear(store, actor="rest:y")
        assert asyncio.run(go()) == 2
        assert count_rows(store) == 0
        rows = asyncio.run(store.audit.read(50))
        cleared = [r for r in rows if r["action"] == "discovery_dismissals_cleared"]
        assert len(cleared) == 1 and cleared[0]["detail"] == "count=2" and cleared[0]["subject"] == "all"
    finally:
        store.close()


def test_rows_survive_a_restart(tmp_path):
    db = tmp_path / "d.db"
    store = open_store(db)
    asyncio.run(dismissals.dismiss(store, "nginx", "d" * 16, actor="x", paths=1))
    store.close()
    store = open_store(db)
    try:
        assert asyncio.run(dismissals.load_fingerprints(store)) == frozenset({("nginx", "d" * 16)})
    finally:
        store.close()


def test_audit_detail_is_counts_only():
    store = open_store()
    try:
        asyncio.run(dismissals.dismiss(store, "nginx", "e" * 16, actor="x", paths=3))
        row = next(r for r in asyncio.run(store.audit.read(50)) if r["action"] == "discovery_dismissed")
        assert row["subject"] == "nginx" and row["detail"] == "paths=3"
    finally:
        store.close()


def test_every_sql_statement_in_dismissals_is_a_string_literal():
    import ast
    src = Path(dismissals.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {t.targets[0].id for t in ast.walk(tree) if isinstance(t, ast.Assign)
              and isinstance(t.targets[0], ast.Name) and isinstance(t.value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "execute", "executemany", "one", "all"):
            arg = node.args[0]
            assert (isinstance(arg, ast.Constant) and isinstance(arg.value, str)) or (
                isinstance(arg, ast.Name) and arg.id in consts), ast.dump(arg)
    assert not re.search(r"f[\"'](SELECT|INSERT|DELETE|UPDATE)", src)
