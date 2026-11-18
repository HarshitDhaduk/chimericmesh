import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.store.errors import MigrationError
from chimericmesh.store.migrations import MigrationRegistry, apply_migrations
from chimericmesh.store.schema import register_core_migrations

SERVICE_VERSIONS = st.lists(st.integers(min_value=100, max_value=199), unique=True, max_size=8)


def fresh() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def build(versions) -> MigrationRegistry:
    reg = MigrationRegistry()
    register_core_migrations(reg)
    for v in versions:
        reg.register(
            "services", v, f"services:{v:04d}",
            (f"CREATE TABLE t{v} (x)", f"INSERT INTO t{v} VALUES ({v})"),
        )
    return reg


def snapshot(c):
    return (
        c.execute("PRAGMA user_version").fetchone()[0],
        sorted(c.execute("SELECT version, name FROM migrations")),
        sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master")),
    )


@settings(max_examples=40, deadline=None)
@given(SERVICE_VERSIONS)
def test_property_16_migration_idempotence(versions):
    c = fresh()
    reg = build(versions)
    apply_migrations(c, reg)
    first = snapshot(c)
    apply_migrations(c, reg)
    assert snapshot(c) == first
    assert first[0] == reg.highest()
    for v in versions:
        assert c.execute(f"SELECT COUNT(*) FROM t{v}").fetchone()[0] == 1


@settings(max_examples=40, deadline=None)
@given(SERVICE_VERSIONS, st.integers(min_value=0, max_value=7), st.data())
def test_property_17_migration_atomicity(versions, extra, data):
    c = fresh()
    base = build(versions)
    apply_migrations(c, base)
    before = snapshot(c)
    bad_version = data.draw(st.integers(min_value=200, max_value=299))
    reg = build(versions)
    stmts = ("CREATE TABLE doomed (x)",) + ("INSERT INTO doomed VALUES (1)",) * extra + ("NOT SQL AT ALL",)
    reg.register("connectors", bad_version, "connectors:bad", stmts)
    with pytest.raises(MigrationError) as ei:
        apply_migrations(c, reg)
    assert "connectors:bad" in str(ei.value)
    assert snapshot(c) == before
    assert not c.in_transaction
