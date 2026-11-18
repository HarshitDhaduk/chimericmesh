"""Transaction handle, counters and subscriber callbacks.

SQL passed to ``Tx`` methods is always a string literal or a module-level constant; values go in
as parameters. Counter names are validated, never interpolated.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager

from .errors import StoreError

_COUNTER_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,47}$")

_SELECT_COUNTER = "SELECT value FROM counters WHERE name = ?"
_SET_COUNTER = (
    "INSERT INTO counters (name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value"
)
_ADD_COUNTER = (
    "INSERT INTO counters (name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value"
)
_RAISE_COUNTER = (
    "INSERT INTO counters (name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = max(value, excluded.value)"
)

# Savepoint names cannot be bound as parameters, so every nesting depth has its own literal.
_SAVEPOINTS: tuple[tuple[str, str, str], ...] = (
    ("SAVEPOINT sp0", "RELEASE sp0", "ROLLBACK TO sp0"),
    ("SAVEPOINT sp1", "RELEASE sp1", "ROLLBACK TO sp1"),
    ("SAVEPOINT sp2", "RELEASE sp2", "ROLLBACK TO sp2"),
    ("SAVEPOINT sp3", "RELEASE sp3", "ROLLBACK TO sp3"),
    ("SAVEPOINT sp4", "RELEASE sp4", "ROLLBACK TO sp4"),
    ("SAVEPOINT sp5", "RELEASE sp5", "ROLLBACK TO sp5"),
    ("SAVEPOINT sp6", "RELEASE sp6", "ROLLBACK TO sp6"),
    ("SAVEPOINT sp7", "RELEASE sp7", "ROLLBACK TO sp7"),
)


class SubscriberError(StoreError):
    """A subscriber callback raised inside a transaction; the caller rolls everything back."""

    def __init__(self, name: str, cause: BaseException) -> None:
        self.name = name
        self.cause = cause
        super().__init__(f"subscriber {name!r} failed: {type(cause).__name__}: {cause}")


def format_id(prefix: str, n: int) -> str:
    return f"{prefix}{int(n)}"


def counter_name_part(s: str) -> str:
    """Make arbitrary text safe to use inside a counter name (for example ``evsrc:<part>``)."""
    return re.sub(r"[^a-z0-9_.-]", "_", s.lower())[:30]


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _COUNTER_NAME.fullmatch(name):
        raise ValueError(f"invalid counter name: {name!r}")
    return name


class Tx:
    """One open transaction on the writer thread. Does not begin or end the transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._depth = 0

    def _cursor(self) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._cursor().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[object]]) -> sqlite3.Cursor:
        return self._cursor().executemany(sql, rows)

    def one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def counter_get(self, name: str) -> int:
        row = self.one(_SELECT_COUNTER, (_check_name(name),))
        return int(row[0]) if row else 0

    def counter_next(self, name: str, *, floor: int = 0) -> int:
        """Store and return ``max(counter, floor) + 1``."""
        value = max(self.counter_get(name), int(floor)) + 1
        self.execute(_SET_COUNTER, (name, value))
        return value

    def counter_add(self, name: str, delta: int) -> int:
        _check_name(name)
        self.execute(_ADD_COUNTER, (name, int(delta)))
        return self.counter_get(name)

    def counter_raise(self, name: str, value: int) -> None:
        self.execute(_RAISE_COUNTER, (_check_name(name), int(value)))

    def counter_set(self, name: str, value: int) -> None:
        """Overwrite a counter (used for the ``clean_shutdown`` flag, which is not a sequence)."""
        self.execute(_SET_COUNTER, (_check_name(name), int(value)))

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        if self._depth >= len(_SAVEPOINTS):
            raise StoreError("savepoints nested too deeply")
        begin, release, rollback = _SAVEPOINTS[self._depth]
        self._conn.execute(begin)
        self._depth += 1
        try:
            yield
        except BaseException:
            self._conn.execute(rollback)
            self._conn.execute(release)
            raise
        else:
            self._conn.execute(release)
        finally:
            self._depth -= 1


def run_callbacks(tx: Tx, callbacks: Iterable[tuple[str, Callable[..., None]]], *args: object) -> None:
    """Run named callbacks in order inside the caller's transaction.

    The first failure raises ``SubscriberError``; the caller's ``fn`` lets it propagate so
    ``transact`` rolls everything back.
    """
    for name, callback in callbacks:
        try:
            callback(tx, *args)
        except Exception as exc:
            raise SubscriberError(name, exc) from exc
