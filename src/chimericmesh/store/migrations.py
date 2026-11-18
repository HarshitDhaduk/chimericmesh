"""Migration registry. Modules register their migrations; the runner applies them at open."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import MigrationError

MODULE_BLOCKS: dict[str, tuple[int, int]] = {
    "store": (1, 99),
    "services": (100, 199),
    "connectors": (200, 299),
    "discovery": (300, 399),
    "approvals": (400, 499),
    "agent": (500, 599),
    "dashboard": (600, 699),
}

Apply = tuple[str, ...] | Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    module: str
    version: int
    name: str
    apply: Apply


class MigrationRegistry:
    def __init__(self) -> None:
        self._by_version: dict[int, Migration] = {}
        self._names: set[str] = set()

    def register(self, module: str, version: int, name: str, apply: Apply) -> None:
        block = MODULE_BLOCKS.get(module)
        if block is None:
            raise MigrationError(name, f"unknown module {module!r}")
        low, high = block
        if not low <= version <= high:
            raise MigrationError(name, f"version {version} is outside the {module} block {low}-{high}")
        if version in self._by_version:
            raise MigrationError(name, f"duplicate version {version}")
        if name in self._names:
            raise MigrationError(name, "duplicate name")
        self._by_version[version] = Migration(module, version, name, apply)
        self._names.add(name)

    def migrations(self) -> list[Migration]:
        return [self._by_version[v] for v in sorted(self._by_version)]

    def highest(self) -> int:
        return max(self._by_version, default=0)


def _set_user_version(conn: sqlite3.Connection, n: int) -> None:
    """The only dynamic SQL in the store: PRAGMA user_version cannot take a parameter."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("user_version must be an int")
    conn.execute(f"PRAGMA user_version = {int(n)}")


_BOOTSTRAP = (
    "CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY, module TEXT NOT NULL, "
    "name TEXT NOT NULL UNIQUE, applied_at REAL NOT NULL)"
)
_INSERT_MIGRATION = "INSERT INTO migrations (version, module, name, applied_at) VALUES (?, ?, ?, ?)"


def _run(conn: sqlite3.Connection, m: Migration, now: Callable[[], float]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if callable(m.apply):
            m.apply(conn)
            if not conn.in_transaction:
                raise MigrationError(
                    m.name,
                    "the migration committed or rolled back by itself; DDL it already committed "
                    "cannot be undone",
                )
        else:
            for statement in m.apply:
                conn.execute(statement)
        conn.execute(_INSERT_MIGRATION, (m.version, m.module, m.name, now()))
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        _set_user_version(conn, max(current, m.version))
        conn.execute("COMMIT")
    except MigrationError:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(m.name, exc) from exc


def apply_migrations(
    conn: sqlite3.Connection, registry: MigrationRegistry, *, now: Callable[[], float] = time.time
) -> None:
    """Apply every registered migration not yet recorded, in ascending version order."""
    conn.execute(_BOOTSTRAP)
    done = {row[0] for row in conn.execute("SELECT version FROM migrations")}
    for m in registry.migrations():
        if m.version not in done:
            _run(conn, m, now)
