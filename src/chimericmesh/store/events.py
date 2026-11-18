"""Event rows: bounded queue, batch writer, retention and queries.

``EventWriter.put`` runs on the loop thread and never blocks. The writer thread calls the hook
methods (``next_wait``, ``on_wake``, ``service``, ``finish``) from its loop in ``writer.py``.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from chimericmesh.events import Event

from .config import StoreConfig
from .tx import Tx, counter_name_part
from .writer import (
    Job,
    WriterState,
    _deliver,
    _execute,
    _past_deadline,
    async_deliver,
    submit,
    sync_deliver,
    wake,
)

log = logging.getLogger("chimericmesh.store")

MAX_TEXT = 4096
WARN_EVERY = 60.0  # seconds between "queue full" warnings

_INSERT = (
    "INSERT OR IGNORE INTO events (id, ts, source, host, app, severity, message, raw, service_id, "
    "log_source_id, ingested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_COUNT = "SELECT COUNT(*) FROM events"
_TTL_DELETE = (
    "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE ingested < ? ORDER BY id LIMIT ?)"
)
_CAP_DELETE = "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT ?)"

UNASSIGNED = "unassigned"
_COLUMNS = (
    "SELECT id, ts, source, host, app, severity, message, raw, service_id, log_source_id FROM events"
)
_AFTER = (
    _COLUMNS + " WHERE id > :after AND (:before IS NULL OR id < :before) AND (:src IS NULL OR source = :src) "
    "AND (:svc IS NULL OR service_id = :svc) ORDER BY id LIMIT :limit"
)
_AFTER_UNASSIGNED = (
    _COLUMNS + " WHERE id > :after AND (:before IS NULL OR id < :before) AND (:src IS NULL OR source = :src) "
    "AND service_id IS NULL ORDER BY id LIMIT :limit"
)
_NEWEST = _COLUMNS + " ORDER BY id DESC LIMIT ?"
_MAX_ID = "SELECT COALESCE(MAX(id), 0) FROM events"

TRIM_CHUNK = 5000  # rows per delete; each chunk is its own transaction
TRIM_SLACK = 500   # a size-cap trim starts when approx_rows passes max_events + this

# Row tuple layout
_ID, _SOURCE = 0, 2


def _text(value: object) -> str:
    """Replace lone surrogates, which sqlite3 cannot bind."""
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _clip(value: object) -> str:
    return _text(value)[:MAX_TEXT]


def _opt(value: object | None) -> str | None:
    return None if value is None else _text(value)


def _make_row(event: Event, ingested: float) -> tuple:
    return (
        event.id or None,  # no id: SQLite assigns one
        float(event.ts), _text(event.source), _text(event.host), _text(event.app), int(event.severity),
        _clip(event.message), _clip(event.raw), _opt(event.service_id), _opt(event.log_source_id),
        float(ingested),
    )


def _insert_batch(tx: Tx, batch: list[tuple], deltas: dict[str, int], last_id: int) -> int:
    """Insert one batch, its counter deltas and the id high-water mark. Returns rows inserted."""
    cur = tx.executemany(_INSERT, batch)
    inserted = cur.rowcount
    for name, delta in deltas.items():
        tx.counter_add(name, delta)
    if last_id:
        tx.counter_raise("event_id", last_id)
    return inserted


def _to_event(row: Any) -> Event:
    return Event(source=row["source"], host=row["host"], app=row["app"], severity=row["severity"],
                 message=row["message"], raw=row["raw"], ts=row["ts"], id=row["id"],
                 service_id=row["service_id"], log_source_id=row["log_source_id"])


def _ttl_chunk(tx: Tx, cutoff: float, n: int) -> int:
    """Delete up to ``n`` of the lowest-id rows ingested before ``cutoff``. Returns rows deleted."""
    return tx.execute(_TTL_DELETE, (cutoff, n)).rowcount


def _cap_chunk(tx: Tx, n: int) -> int:
    """Delete the ``n`` lowest-id rows. Returns rows deleted."""
    return tx.execute(_CAP_DELETE, (n,)).rowcount


class EventWriter:
    def __init__(self, state: WriterState, config: StoreConfig, now: Callable[[], float],
                 previous_shutdown_clean: bool) -> None:
        self._state = state
        self._cfg = config
        self._now = now
        self.previous_shutdown_clean = previous_shutdown_clean
        self._lock = threading.Lock()
        # (seq, monotonic time of put, row)
        self._rows: collections.deque[tuple[int, float, tuple]] = collections.deque()
        self._seq = 0          # sequence number of the newest accepted row
        self._resolved = 0     # every row with a sequence number <= this is committed or dropped
        self._orphans: dict[str, int] = {}   # counter deltas of rows that never reach a batch
        self._waiters: list[tuple[int, asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._wake_sent = False
        self._closed = False
        self._last_warn: float | None = None
        self.dropped_events = 0
        # retention (writer thread only)
        self._approx: int | None = None  # row count estimate; None until the first count
        self._last_trim = time.monotonic()
        self._trim: str | None = None  # None, "ttl" or "cap" while a trim is running
        self._trim_cutoff = 0.0
        self._trim_remaining = 0
        self._last_was_flush = False

    # ---- loop thread ---------------------------------------------------------------------------

    def put(self, event: Event) -> None:
        """Queue one row. Never blocks and never raises."""
        try:
            row = _make_row(event, self._now())
            send_wake = False
            with self._lock:
                if self._closed or self._state.stopping:
                    self._note_drop(1, "after close")
                    return
                if len(self._rows) >= self._cfg.queue_limit:
                    _, _, old = self._rows.popleft()
                    self._add_orphan(old)
                    self._note_drop(1, "queue full")
                self._seq += 1
                self._rows.append((self._seq, time.monotonic(), row))
                if not self._wake_sent:
                    self._wake_sent = send_wake = True
            if send_wake and not wake(self._state):
                with self._lock:
                    self._wake_sent = False
        except Exception:  # put must never break ingest
            log.exception("store: could not queue an event row")
            self.dropped_events += 1

    @property
    def crash_margin(self) -> int:
        """Ids that may have been handed out but not stored when the previous run crashed (L2)."""
        return 2 * self._cfg.queue_limit + self._cfg.batch_rows

    def queue_depth(self) -> int:
        return len(self._rows)

    async def barrier(self) -> None:
        """Return once every row queued so far is committed or dropped (forces a flush)."""
        loop = asyncio.get_running_loop()
        with self._lock:
            target = self._seq
            if self._closed or target <= self._resolved:
                return
            fut: asyncio.Future = loop.create_future()
            self._waiters.append((target, loop, fut))
            self._wake_sent = True
        if not wake(self._state):
            self._resolve(self._seq)
        await fut

    # ---- shared helpers ------------------------------------------------------------------------

    def _note_drop(self, n: int, why: str) -> None:
        """Count dropped rows and warn at most once a minute. Caller may hold the lock."""
        self.dropped_events += n
        now = self._now()
        if self._last_warn is None or now - self._last_warn >= WARN_EVERY:
            self._last_warn = now
            log.warning("store: event rows dropped (%s); %d dropped so far", why, self.dropped_events)

    def _add_orphan(self, row: tuple) -> None:
        for name in ("events_total", "evsrc:" + counter_name_part(row[_SOURCE])):
            self._orphans[name] = self._orphans.get(name, 0) + 1

    def _forced(self) -> bool:
        return any(target > self._resolved for target, _, _ in self._waiters)

    def _resolve(self, upto: int) -> None:
        with self._lock:
            self._resolved = max(self._resolved, upto)
            ready = [w for w in self._waiters if w[0] <= self._resolved]
            self._waiters = [w for w in self._waiters if w[0] > self._resolved]
        for _, loop, fut in ready:
            _deliver(loop, fut, None, None)

    # ---- writer thread hooks -------------------------------------------------------------------

    def on_wake(self) -> None:
        with self._lock:
            self._wake_sent = False

    def next_wait(self) -> float | None:
        """Seconds the writer may sleep before it must look at the queue again (None = forever)."""
        if self._trim is not None:
            return 0.0
        trim_due = max(0.0, self._last_trim + self._cfg.trim_interval - time.monotonic())
        with self._lock:
            if not self._rows:
                return trim_due
            if len(self._rows) >= self._cfg.batch_rows or self._forced():
                return 0.0
            due = self._rows[0][1] + self._cfg.batch_ms / 1000.0
        return min(trim_due, max(0.0, due - time.monotonic()))

    def _flush_due(self) -> bool:
        with self._lock:
            if not self._rows:
                return False
            if len(self._rows) >= self._cfg.batch_rows or self._forced():
                return True
            return time.monotonic() - self._rows[0][1] >= self._cfg.batch_ms / 1000.0

    def service(self, state: WriterState) -> None:
        """One unit of work per call, so queued jobs run between batches and trim chunks."""
        if self._trim is None and time.monotonic() - self._last_trim >= self._cfg.trim_interval:
            self._start_trim()
        if self._flush_due() and not (self._trim is not None and self._last_was_flush):
            self._flush_one(state)
            self._last_was_flush = True
            if (self._trim is None and self._approx is not None
                    and self._approx > self._cfg.max_events + TRIM_SLACK):
                self._start_trim()
            return
        self._last_was_flush = False
        if self._trim is not None:
            self._trim_step(state)

    # ---- retention -----------------------------------------------------------------------------

    def _job(self, state: WriterState, fn: Callable[[Tx], Any], write: bool) -> tuple[Any, BaseException | None]:
        box: list[tuple[Any, BaseException | None]] = []
        _execute(state, Job(fn, write, lambda result, exc: box.append((result, exc))))
        return box[0]

    def _start_trim(self) -> None:
        self._trim = "ttl"
        self._trim_cutoff = self._now() - self._cfg.event_ttl_days * 86400.0
        self._last_trim = time.monotonic()

    def _trim_step(self, state: WriterState) -> None:
        """Run one chunk of the TTL loop, then of the size-cap loop (stop rule in the design)."""
        if self._trim == "ttl":
            cutoff = self._trim_cutoff

            def ttl(tx: Tx) -> tuple[int, int | None]:
                n = _ttl_chunk(tx, cutoff, TRIM_CHUNK)
                return n, (int(tx.one(_COUNT)[0]) if n < TRIM_CHUNK else None)

            result, exc = self._job(state, ttl, True)
            if exc is not None:
                log.error("store: event trim failed (%s)", type(exc).__name__)
                self._trim = None
                return
            n, total = result
            if self._approx is not None:
                self._approx = max(0, self._approx - n)
            if n < TRIM_CHUNK:
                self._approx = total
                self._trim_remaining = total - self._cfg.max_events
                self._trim = "cap" if self._trim_remaining > 0 else None
            return
        remaining = self._trim_remaining
        result, exc = self._job(state, lambda tx: _cap_chunk(tx, min(TRIM_CHUNK, remaining)), True)
        if exc is not None:
            log.error("store: event trim failed (%s)", type(exc).__name__)
            self._trim = None
            return
        if self._approx is not None:
            self._approx = max(0, self._approx - result)
        self._trim_remaining -= result
        if result == 0 or self._trim_remaining <= 0:
            self._trim = None
    def _take_batch(self) -> tuple[list[tuple[int, float, tuple]], dict[str, int]]:
        with self._lock:
            n = min(len(self._rows), self._cfg.batch_rows)
            taken = [self._rows.popleft() for _ in range(n)]
            orphans, self._orphans = self._orphans, {}
        return taken, orphans

    def _flush_one(self, state: WriterState) -> None:
        if self._approx is None:
            total, exc = self._job(state, lambda tx: int(tx.one(_COUNT)[0]), False)
            if exc is None:
                self._approx = total
        taken, orphans = self._take_batch()
        if not taken:
            return
        batch = [row for _, _, row in taken]
        deltas = dict(orphans)
        deltas["events_total"] = deltas.get("events_total", 0) + len(batch)
        for row in batch:
            name = "evsrc:" + counter_name_part(row[_SOURCE])
            deltas[name] = deltas.get(name, 0) + 1
        last_id = max((row[_ID] or 0 for row in batch), default=0)
        box: list[tuple[Any, BaseException | None]] = []
        _execute(state, Job(lambda tx: _insert_batch(tx, batch, deltas, last_id), True,
                            lambda result, exc: box.append((result, exc))))
        result, exc = box[0]
        if exc is not None:
            with self._lock:
                self._note_drop(len(batch), "write failed")
                for name, delta in deltas.items():
                    self._orphans[name] = self._orphans.get(name, 0) + delta
            log.error("store: dropped a batch of %d event rows after the retries (%s)",
                      len(batch), type(exc).__name__)
        else:
            ignored = len(batch) - int(result)
            if self._approx is not None:
                self._approx += int(result)
            if ignored > 0:
                with self._lock:
                    self._note_drop(ignored, "duplicate event id")
                log.error("store: %d event rows ignored (duplicate ids)", ignored)
        self._resolve(taken[-1][0])

    def finish(self, state: WriterState) -> None:
        """Shutdown: drain until the deadline (unless abandoned), then drop what is left."""
        if not state.abandon:
            while True:
                with self._lock:
                    pending = bool(self._rows)
                if not pending or _past_deadline(state):
                    break
                self._flush_one(state)
        with self._lock:
            left = len(self._rows)
            for _, _, row in self._rows:
                self._add_orphan(row)
            self._rows.clear()
            self._closed = True
            if left:
                self._note_drop(left, "shutdown")
        self._resolve(self._seq)

    # ---- queries (run on the writer thread through the job queue) ------------------------------

    async def _read(self, fn: Callable[[Tx], Any]) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        submit(self._state, Job(fn, False, async_deliver(loop, future)))
        return await future

    def _read_sync(self, fn: Callable[[Tx], Any]) -> Any:
        future: concurrent.futures.Future = concurrent.futures.Future()
        submit(self._state, Job(fn, False, sync_deliver(future)))
        return future.result()

    async def after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                    service_id: str | None = None, before_id: int | None = None) -> list[Event]:
        """Events with an id above ``after_id``, oldest first, at most ``limit``."""
        params = {"after": int(after_id), "before": before_id, "src": source, "svc": service_id,
                  "limit": max(0, int(limit))}
        if service_id == UNASSIGNED:
            return await self._read(lambda tx: [_to_event(r) for r in tx.all(_AFTER_UNASSIGNED, params)])  # type: ignore[arg-type]
        return await self._read(lambda tx: [_to_event(r) for r in tx.all(_AFTER, params)])  # type: ignore[arg-type]

    async def newest(self, limit: int) -> list[Event]:
        """The newest ``limit`` events, oldest first (startup refill of the ring)."""
        n = max(0, int(limit))
        rows = await self._read(lambda tx: tx.all(_NEWEST, (n,)))
        return [_to_event(r) for r in reversed(rows)]

    def max_id_sync(self) -> int:
        """Startup seed for event ids. Blocks the caller; runs on the writer thread."""
        def fn(tx: Tx) -> int:
            return max(int(tx.one(_MAX_ID)[0]), tx.counter_get("event_id"))

        seed = self._read_sync(fn)
        return seed if self.previous_shutdown_clean else seed + self.crash_margin