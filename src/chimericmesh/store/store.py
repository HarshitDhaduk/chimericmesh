"""The ``Store`` facade: open, transact, read, health, close."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sqlite3
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from chimericmesh import fsperms

from .audit import AuditLog
from .config import MEMORY, StoreConfig
from .connection import open_connection
from .events import EventWriter
from .migrations import MigrationRegistry, apply_migrations
from .tx import Tx
from .writer import (
    Job,
    WriterState,
    _stop_writer,
    async_deliver,
    stop,
    submit,
    sync_deliver,
)

log = logging.getLogger("chimericmesh.store")

T = TypeVar("T")

_SELECT_CLEAN = "SELECT value FROM counters WHERE name = 'clean_shutdown'"
_CLEAN_NAME = "clean_shutdown"


@dataclass(frozen=True)
class StoreHealth:
    store_ok: bool
    dropped_events: int
    queue_depth: int
    path: str


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Store:
    """One SQLite connection owned by one writer thread (started on the first job)."""

    path: str
    config: StoreConfig
    previous_shutdown_clean: bool

    def __init__(self, conn: sqlite3.Connection, path: str, config: StoreConfig,
                 now: Callable[[], float], previous_shutdown_clean: bool) -> None:
        self.path = path
        self.config = config
        self.now = now
        self.previous_shutdown_clean = previous_shutdown_clean
        self._state = WriterState(conn, path)
        self._last_event_id = 0
        self._closed = False
        self.audit = AuditLog(self)
        self.events = EventWriter(self._state, config, now, previous_shutdown_clean)
        self._state.hook = self.events
        # The finalizer sees only the state, never the store, so an unreferenced store stops its thread.
        self._finalizer = weakref.finalize(self, _stop_writer, self._state)

    @classmethod
    def open(cls, path: str | os.PathLike[str], *, registry: MigrationRegistry,
             config: StoreConfig | None = None, now: Callable[[], float] = time.time) -> "Store":
        text = os.fspath(path)
        resolved = text if text == MEMORY else os.path.abspath(text)
        conn = open_connection(resolved, registry)
        try:
            apply_migrations(conn, registry, now=now)
            clean = cls._mark_running(conn)
            if resolved != MEMORY:
                fsperms.secure_sidecars(resolved)
        except BaseException:
            conn.close()
            raise
        return cls(conn, resolved, config or StoreConfig.from_env(), now, clean)

    @staticmethod
    def _mark_running(conn: sqlite3.Connection) -> bool:
        """Read the previous shutdown flag, then set it to 0. A new database counts as clean."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(_SELECT_CLEAN).fetchone()
            previous_clean = row is None or row[0] == 1
            Tx(conn).counter_set(_CLEAN_NAME, 0)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return previous_clean

    # ---- running work ----------------------------------------------------------------------

    async def _run(self, fn: Callable[[Tx], T], write: bool) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        submit(self._state, Job(fn, write, async_deliver(loop, future)))
        return await future

    def _run_sync(self, fn: Callable[[Tx], T], write: bool, allow_blocking: bool) -> T:
        if not allow_blocking and _in_running_loop():
            raise RuntimeError("blocking store call from a running event loop; use the async API")
        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        submit(self._state, Job(fn, write, sync_deliver(future)))
        return future.result()

    async def transact(self, fn: Callable[[Tx], T]) -> T:
        """Run ``fn`` in one write transaction; returns after COMMIT. ``fn`` may be called up to
        four times on a retryable error, so it must have no side effects outside ``tx``."""
        return await self._run(fn, True)

    async def read(self, fn: Callable[[Tx], T]) -> T:
        return await self._run(fn, False)

    def transact_sync(self, fn: Callable[[Tx], T], *, allow_blocking: bool = False) -> T:
        return self._run_sync(fn, True, allow_blocking)

    def read_sync(self, fn: Callable[[Tx], T], *, allow_blocking: bool = False) -> T:
        return self._run_sync(fn, False, allow_blocking)

    # ---- state -----------------------------------------------------------------------------

    def health(self) -> StoreHealth:
        return StoreHealth(self._state.store_ok, self.events.dropped_events, self.events.queue_depth(), self.path)

    async def note_last_event_id(self, n: int) -> None:
        """Raise the durable ``event_id`` high-water mark to ``n`` (never lowers it)."""
        self._last_event_id = max(self._last_event_id, int(n))
        self._state.last_event_id = self._last_event_id
        await self.transact(lambda tx: tx.counter_raise("event_id", n))

    def add_redaction(self, secret: str) -> None:
        """Audit rows never contain `secret`; it is replaced by `[redacted]`."""
        self.audit._add_secret(secret)

    # ---- shutdown --------------------------------------------------------------------------

    def close(self, timeout: float = 5.0, *, clean: bool = True) -> None:
        """Stop intake, drain, checkpoint and join. Harmless to call twice."""
        self._closed = True
        self._state.last_event_id = max(self._state.last_event_id, self._last_event_id)
        stop(self._state, clean=clean, timeout=timeout)

    def _abandon(self) -> None:
        """Test hook: simulate a crash. No drain, ``clean_shutdown`` stays 0."""
        self._closed = True
        stop(self._state, clean=False, timeout=5.0, abandon=True)
