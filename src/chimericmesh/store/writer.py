"""The single writer thread. After ``Store.open`` every read and write runs here.

``WriterState`` holds everything the thread needs and no reference to the ``Store``, so a
``weakref.finalize`` on the store can stop the thread of an unreferenced store.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import (
    StoreBusyError,
    StoreClosedError,
    StoreError,
    StoreFullError,
    StoreReentryError,
    StoreWriteError,
)
from .tx import Tx

log = logging.getLogger("chimericmesh.store")

THREAD_NAME = "store-writer"
RETRY_DELAYS: tuple[float, ...] = (0.05, 0.2, 0.8)  # three retries after the first try (T8)

_STOP = object()
_WAKE = object()  # an event row is waiting; the hook decides when to flush

_BEGIN = "BEGIN IMMEDIATE"
_COMMIT = "COMMIT"
_ROLLBACK = "ROLLBACK"
_CHECKPOINT = "PRAGMA wal_checkpoint(TRUNCATE)"
_SET_CLEAN = (
    "INSERT INTO counters (name, value) VALUES ('clean_shutdown', 1) "
    "ON CONFLICT(name) DO UPDATE SET value = 1"
)
_RAISE_EVENT_ID = (
    "INSERT INTO counters (name, value) VALUES ('event_id', ?) "
    "ON CONFLICT(name) DO UPDATE SET value = max(value, excluded.value)"
)


@dataclass
class Job:
    fn: Callable[[Tx], Any]
    write: bool
    deliver: Callable[[Any, BaseException | None], None]


class WriterState:
    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self.conn = conn
        self.path = path
        self.queue: queue.Queue[Any] = queue.Queue()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stopping = False  # no new jobs once set
        self.abandon = False
        self.clean = True
        self.deadline: float | None = None
        self.store_ok = True
        self.last_event_id = 0
        self.hook: Any = None  # EventWriter: batching, trimming and drain on shutdown
        self.finished = threading.Event()


# ---- delivery ------------------------------------------------------------------------------

def _set(fut: asyncio.Future, result: Any, exc: BaseException | None) -> None:
    if fut.done():  # cancelled or already finished
        return
    try:
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)
    except asyncio.InvalidStateError:
        pass


def _deliver(loop: asyncio.AbstractEventLoop, future: asyncio.Future, result: Any,
             exc: BaseException | None) -> None:
    """Hand a result to the loop thread. Never raises on the writer thread."""
    try:
        loop.call_soon_threadsafe(_set, future, result, exc)
    except RuntimeError:  # loop already closed
        log.debug("result dropped: event loop is closed")


def async_deliver(loop: asyncio.AbstractEventLoop, future: asyncio.Future):
    return lambda result, exc: _deliver(loop, future, result, exc)


def sync_deliver(future: concurrent.futures.Future):
    def deliver(result: Any, exc: BaseException | None) -> None:
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    return deliver


# ---- running jobs --------------------------------------------------------------------------

def _classify(exc: sqlite3.Error) -> str | None:
    """'busy', 'full', 'io' (retryable), 'corrupt' (fatal), or None (not a store failure)."""
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    msg = str(exc).lower()
    if primary in (11, 26) or "malformed" in msg or "not a database" in msg:
        return "corrupt"
    if primary in (5, 6) or "locked" in msg or "busy" in msg:
        return "busy"
    if primary == 13 or "disk is full" in msg or "database or disk is full" in msg:
        return "full"
    if primary == 10 or "disk i/o" in msg:
        return "io"
    return None


_ERRORS = {"busy": StoreBusyError, "full": StoreFullError, "io": StoreWriteError, "corrupt": StoreWriteError}


def _run_once(conn: sqlite3.Connection, job: Job) -> Any:
    if job.write:
        conn.execute(_BEGIN)
    try:
        result = job.fn(Tx(conn))
        if job.write:
            conn.execute(_COMMIT)
        return result
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute(_ROLLBACK)
            except sqlite3.Error:
                pass
        raise


def _set_ok(state: WriterState, ok: bool, why: str = "") -> None:
    if state.store_ok != ok:
        state.store_ok = ok
        if ok:
            log.warning("store: writes are succeeding again (%s)", state.path)
        # the failure itself is logged where it happens


def _execute(state: WriterState, job: Job) -> None:
    delays = RETRY_DELAYS
    for attempt in range(len(delays) + 1):
        try:
            result = _run_once(state.conn, job)
        except sqlite3.Error as exc:
            kind = _classify(exc)
            if kind is None:
                job.deliver(None, exc)
                return
            if kind == "corrupt":
                _set_ok(state, False)
                log.critical("store: database error (%s) on %s", type(exc).__name__, state.path)
                job.deliver(None, StoreWriteError(f"store write failed: {type(exc).__name__}"))
                return
            if attempt < len(delays):
                log.warning("store: %s on %s, retry %d", kind, state.path, attempt + 1)
                time.sleep(delays[attempt])
                continue
            _set_ok(state, False)
            log.error("store: write failed after %d tries (%s, %s)", attempt + 1, kind, state.path)
            job.deliver(None, _ERRORS[kind](f"store write failed: {kind}"))
            return
        except BaseException as exc:  # caller's own error, not retried
            job.deliver(None, exc)
            return
        if job.write:
            _set_ok(state, True)
        job.deliver(result, None)
        return


def _final_work(state: WriterState) -> None:
    """Runs on the writer thread (or inline when none started) before the connection closes."""
    conn = state.conn
    try:
        if not state.abandon:
            if state.last_event_id:
                conn.execute(_BEGIN)
                conn.execute(_RAISE_EVENT_ID, (state.last_event_id,))
                conn.execute(_COMMIT)
            if state.clean:
                conn.execute(_BEGIN)
                conn.execute(_SET_CLEAN)
                conn.execute(_COMMIT)
            conn.execute(_CHECKPOINT)
    except sqlite3.Error as exc:
        log.error("store: shutdown work failed (%s): %s", state.path, type(exc).__name__)
        if conn.in_transaction:
            try:
                conn.execute(_ROLLBACK)
            except sqlite3.Error:
                pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        state.finished.set()


def _past_deadline(state: WriterState) -> bool:
    return state.deadline is not None and time.monotonic() > state.deadline


def _service(state: WriterState, hook: Any) -> None:
    try:
        hook.service(state)
    except Exception:  # the writer thread must survive a bug in the hook
        log.exception("store: event writer hook failed (%s)", state.path)


def _run(state: WriterState) -> None:
    hook = state.hook
    while True:
        timeout = hook.next_wait() if hook is not None else None
        try:
            item = state.queue.get(timeout=timeout)
        except queue.Empty:
            item = None
        if item is _STOP:
            break
        if item is _WAKE:
            hook.on_wake()
        elif item is not None:
            if state.abandon or _past_deadline(state):
                item.deliver(None, StoreClosedError("store is closing"))
            else:
                _execute(state, item)
        if hook is not None and not state.abandon:
            _service(state, hook)
    _finish(state)
    _final_work(state)


def _finish(state: WriterState) -> None:
    if state.hook is None:
        return
    try:
        state.hook.finish(state)
    except Exception:
        log.exception("store: draining event rows failed (%s)", state.path)

# ---- control -------------------------------------------------------------------------------

def submit(state: WriterState, job: Job) -> None:
    if state.thread is not None and threading.current_thread() is state.thread:
        raise StoreReentryError("a store job was submitted from the writer thread")
    with state.lock:
        if state.stopping:
            raise StoreClosedError("store is closed")
        if state.thread is None:
            state.thread = threading.Thread(target=_run, args=(state,), name=THREAD_NAME, daemon=True)
            state.thread.start()
        state.queue.put(job)


def wake(state: WriterState) -> bool:
    """Tell the writer thread that event rows are waiting (starts it if needed). False when closed."""
    with state.lock:
        if state.stopping:
            return False
        if state.thread is None:
            state.thread = threading.Thread(target=_run, args=(state,), name=THREAD_NAME, daemon=True)
            state.thread.start()
        state.queue.put(_WAKE)
    return True


def stop(state: WriterState, *, clean: bool, timeout: float, abandon: bool = False) -> bool:
    """Stop intake, drain until the deadline, finish on the writer thread. True when finished."""
    with state.lock:
        first = not state.stopping
        if first:
            state.stopping = True
            state.clean = clean
            state.abandon = abandon
            state.deadline = time.monotonic() + max(timeout, 0.0)
            if state.thread is None:
                _finish(state)
                _final_work(state)  # nothing ever ran: finish inline on the caller's thread
            else:
                state.queue.put(_STOP)
        thread = state.thread
    if thread is None or thread is threading.current_thread():
        return state.finished.is_set()
    thread.join(max(timeout, 0.0) if first else 0.0)
    if thread.is_alive():
        if first:
            log.error("store: writer did not finish within %.1fs; it will close the database when it unblocks",
                      timeout)
        return False
    return True


def _stop_writer(state: WriterState) -> None:
    """weakref.finalize target: the store was dropped without close()."""
    stop(state, clean=False, timeout=1.0)
