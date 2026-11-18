import asyncio
import gc
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chimericmesh.store import writer as writer_mod
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.errors import (
    StoreBusyError,
    StoreClosedError,
    StoreFullError,
    StoreReentryError,
    StoreWriteError,
)
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store
from chimericmesh.store.tx import SubscriberError, run_callbacks

WRITER = "store-writer"


def open_store(path=":memory:", **cfg) -> Store:
    config = StoreConfig.from_env({}, **cfg) if cfg else None
    return Store.open(path, registry=core_registry(), config=config)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(writer_mod, "RETRY_DELAYS", (0.0, 0.0, 0.0))


def writer_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == WRITER)


def counter(store: Store, name: str) -> int:
    return store.read_sync(lambda tx: tx.counter_get(name))


def run(coro):
    return asyncio.run(coro)


# ---- transact / read -------------------------------------------------------------------------

def test_transact_returns_after_commit_and_is_visible_to_another_store(tmp_path):
    path = tmp_path / "s.db"
    a = open_store(path)
    b = open_store(path)
    try:
        assert run(a.transact(lambda tx: tx.counter_add("x", 5))) == 5
        assert counter(b, "x") == 5
    finally:
        a.close()
        b.close()


def test_exception_in_fn_rolls_back_and_propagates():
    s = open_store()

    def fn(tx):
        tx.counter_add("x", 1)
        raise KeyError("boom")

    with pytest.raises(KeyError):
        run(s.transact(fn))
    assert counter(s, "x") == 0
    s.close()


def test_read_runs_without_begin_and_returns_value():
    s = open_store()
    seen = []

    def fn(tx):
        seen.append(tx.execute("SELECT 1").connection.in_transaction)
        return 42

    assert run(s.read(fn)) == 42
    assert seen == [False]
    s.close()


def test_sync_variants_return_values():
    s = open_store()
    assert s.transact_sync(lambda tx: tx.counter_add("y", 2)) == 2
    assert s.read_sync(lambda tx: tx.counter_get("y")) == 2
    s.close()


# ---- retries ---------------------------------------------------------------------------------

def flaky(failures: int, message="database is locked"):
    calls = []

    def fn(tx):
        calls.append(1)
        if len(calls) <= failures:
            raise sqlite3.OperationalError(message)
        return tx.counter_add("ok", 1)

    return fn, calls


def test_two_injected_failures_are_retried_then_succeed():
    s = open_store()
    fn, calls = flaky(2)
    assert run(s.transact(fn)) == 1
    assert len(calls) == 3
    s.close()


def test_exactly_three_failures_still_succeed_on_the_fourth_call():  # L1
    s = open_store()
    fn, calls = flaky(3)
    assert run(s.transact(fn)) == 1
    assert len(calls) == 4
    s.close()


def test_four_failures_raise_busy_and_flip_store_ok(caplog):
    s = open_store()
    fn, calls = flaky(4)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(StoreBusyError):
            run(s.transact(fn))
    assert len(calls) == 4
    assert s.health().store_ok is False
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    run(s.transact(lambda tx: tx.counter_add("z", 1)))
    assert s.health().store_ok is True
    s.close()


def test_disk_full_maps_to_store_full_error():
    s = open_store()
    fn, _ = flaky(10, "database or disk is full")
    with pytest.raises(StoreFullError):
        run(s.transact(fn))
    assert s.health().store_ok is False
    s.close()


def test_disk_io_error_maps_to_write_error():
    s = open_store()
    fn, _ = flaky(10, "disk I/O error")
    with pytest.raises(StoreWriteError):
        run(s.transact(fn))
    s.close()


def test_corruption_is_not_retried_and_keeps_store_ok_false():
    s = open_store()
    fn, calls = flaky(10, "database disk image is malformed")
    with pytest.raises(StoreWriteError):
        run(s.transact(fn))
    assert len(calls) == 1
    assert s.health().store_ok is False
    s.close()


def test_non_retryable_errors_propagate_unchanged_without_retry():
    s = open_store()
    calls = []

    def fn(tx):
        calls.append(1)
        tx.execute("INSERT INTO counters VALUES ('a', 1)")
        tx.execute("INSERT INTO counters VALUES ('a', 2)")

    with pytest.raises(sqlite3.IntegrityError):
        run(s.transact(fn))
    assert len(calls) == 1 and counter(s, "a") == 0
    assert s.health().store_ok is True
    s.close()


# ---- thread rules ----------------------------------------------------------------------------

def test_sync_call_from_running_loop_raises_unless_allowed():
    s = open_store()

    async def main():
        with pytest.raises(RuntimeError):
            s.read_sync(lambda tx: 1)
        with pytest.raises(RuntimeError):
            s.transact_sync(lambda tx: 1)
        assert s.read_sync(lambda tx: 1, allow_blocking=True) == 1

    run(main())
    s.close()


def test_job_submitted_from_the_writer_thread_raises_reentry_error():
    s = open_store()

    def inner(tx):
        return s.read_sync(lambda t: 1)

    with pytest.raises(StoreReentryError):
        s.transact_sync(inner)

    def fn(tx):
        asyncio.run(s.read(lambda t: 1))  # the async API refuses it too

    with pytest.raises(StoreReentryError):
        s.transact_sync(fn)
    s.close()


def test_raising_callback_inside_transact_rolls_the_whole_transaction_back():  # Property 21 example
    s = open_store()

    def good(tx):
        tx.counter_add("from_good", 1)

    def bad(tx):
        raise ValueError("no")

    def fn(tx):
        tx.counter_add("before", 1)
        run_callbacks(tx, [("good", good), ("bad", bad)])

    with pytest.raises(SubscriberError) as ei:
        run(s.transact(fn))
    assert ei.value.name == "bad"
    assert counter(s, "before") == 0 and counter(s, "from_good") == 0
    s.close()


# ---- close and lifecycle ---------------------------------------------------------------------

def test_close_is_fast_idempotent_and_blocks_further_use(tmp_path):
    s = open_store(tmp_path / "c.db")
    s.read_sync(lambda tx: 1)
    t0 = time.monotonic()
    s.close(timeout=5)
    assert time.monotonic() - t0 < 5
    s.close()
    with pytest.raises(StoreClosedError):
        s.read_sync(lambda tx: 1)
    with pytest.raises(StoreClosedError):
        run(s.transact(lambda tx: 1))


def test_memory_store_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = open_store(":memory:")
    s.transact_sync(lambda tx: tx.counter_add("m", 1))
    s.close()
    assert list(tmp_path.iterdir()) == []
    assert s.path == ":memory:"


def test_path_is_absolute_for_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = open_store("rel.db")
    try:
        assert s.path == str(tmp_path / "rel.db")
    finally:
        s.close()


def test_writer_starts_lazily_on_the_writer_thread_and_is_a_daemon():  # M4
    before = writer_threads()
    s = open_store()
    assert writer_threads() == before  # open runs on the opening thread, no writer yet
    names = []
    s.read_sync(lambda tx: names.append(threading.current_thread()))
    th = names[0]
    assert th.name == WRITER and th.daemon and th is not threading.current_thread()
    assert writer_threads() == before + 1
    s.close()
    assert writer_threads() == before


def test_unclosed_stores_do_not_leak_threads():  # L3
    gc.collect()
    start = threading.active_count()
    stores = [open_store() for _ in range(50)]
    for s in stores:
        s.read_sync(lambda tx: 1)
    assert threading.active_count() >= start + 50
    del stores, s
    gc.collect()
    assert threading.active_count() == start


def test_unclosed_store_does_not_keep_the_interpreter_alive(tmp_path):
    code = (
        "from chimericmesh.store.schema import core_registry\n"
        "from chimericmesh.store.store import Store\n"
        "s = Store.open(':memory:', registry=core_registry())\n"
        "s.read_sync(lambda tx: 1)\n"
        "print('ok')\n"
    )
    import os

    env = dict(os.environ, PYTHONPATH=str(Path(writer_mod.__file__).resolve().parents[2]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5, env=env)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr


def test_result_delivered_to_a_closed_loop_does_not_crash_the_writer():
    s = open_store()
    errors = []
    old = threading.excepthook
    threading.excepthook = lambda args: errors.append(args)
    try:
        for i in range(2):  # asyncio.run twice on one store
            assert run(s.transact(lambda tx: tx.counter_add("loops", 1))) == i + 1
        # deliver to a loop that is closed by the time the job finishes
        gate = threading.Event()
        loop = asyncio.new_event_loop()
        fut_holder = []

        async def start():
            fut_holder.append(asyncio.ensure_future(s.transact(lambda tx: (gate.wait(5), tx.counter_add("late", 1))[1])))
            await asyncio.sleep(0.05)

        loop.run_until_complete(start())
        loop.close()  # loop dies while the writer is still busy
        gate.set()
        assert s.read_sync(lambda tx: tx.counter_get("late")) in (0, 1)
        assert errors == []
    finally:
        threading.excepthook = old
    s.close()


def test_result_delivered_to_a_cancelled_awaiter_is_ignored(caplog):
    s = open_store()
    gate, entered = threading.Event(), threading.Event()
    errors = []
    old = threading.excepthook
    threading.excepthook = lambda args: errors.append(args)

    def slow(tx):
        entered.set()
        gate.wait(5)
        return tx.counter_add("cancelled", 1)

    async def main():
        task = asyncio.ensure_future(s.transact(slow))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.sleep(0.1)

    try:
        with caplog.at_level(logging.ERROR):
            run(main())
        assert s.read_sync(lambda tx: tx.counter_get("cancelled")) == 1  # committed anyway
        assert errors == [] and caplog.records == []
    finally:
        threading.excepthook = old
    s.close()


def test_close_with_a_stuck_writer_returns_quickly_and_the_writer_closes_the_connection(caplog):  # L4
    s = open_store()
    gate, entered = threading.Event(), threading.Event()
    result = []

    def stuck(tx):
        entered.set()
        gate.wait(10)
        return "done"

    t = threading.Thread(target=lambda: result.append(s.transact_sync(stuck)))
    t.start()
    assert entered.wait(5)
    state = s._state
    t0 = time.monotonic()
    with caplog.at_level(logging.ERROR):
        s.close(timeout=0.2)
    assert time.monotonic() - t0 < 1.0
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    # not closed from the calling thread: the connection still works from the writer's point of view
    assert state.thread.is_alive()
    gate.set()
    t.join(5)
    state.thread.join(5)
    assert not state.thread.is_alive()
    assert result == ["done"]
    with pytest.raises(sqlite3.ProgrammingError):
        state.conn.execute("SELECT 1")


# ---- shutdown flag and event id mark ---------------------------------------------------------

def test_previous_shutdown_clean_semantics(tmp_path):
    path = tmp_path / "f.db"
    s = open_store(path)
    assert s.previous_shutdown_clean is True  # new database
    s.read_sync(lambda tx: 1)
    s.close()
    s = open_store(path)
    assert s.previous_shutdown_clean is True
    s._abandon()
    s = open_store(path)
    assert s.previous_shutdown_clean is False
    s.close()
    s = open_store(path)
    assert s.previous_shutdown_clean is True
    s.close()


def test_abandon_without_a_writer_thread_also_leaves_flag_unset(tmp_path):
    path = tmp_path / "g.db"
    s = open_store(path)
    s._abandon()
    s = open_store(path)
    assert s.previous_shutdown_clean is False
    s.close()


def test_close_clean_false_leaves_flag_at_zero(tmp_path):  # L14
    path = tmp_path / "h.db"
    s = open_store(path)
    s.close(clean=False)
    s = open_store(path)
    assert s.previous_shutdown_clean is False
    assert counter(s, "clean_shutdown") == 0
    s.close()
    s = open_store(path)
    assert counter(s, "clean_shutdown") == 0  # open sets it to 0 again; close set 1 before
    assert s.previous_shutdown_clean is True
    s.close()


def test_note_last_event_id_raises_and_never_lowers(tmp_path):
    path = tmp_path / "e.db"
    s = open_store(path)
    run(s.note_last_event_id(50))
    run(s.note_last_event_id(20))
    assert counter(s, "event_id") == 50
    s.close()


def test_close_repeats_the_event_id_raise(tmp_path):
    path = tmp_path / "e2.db"
    s = open_store(path)
    s._last_event_id = 77  # as note_last_event_id sets it before submitting its write
    s.close()
    s = open_store(path)
    assert counter(s, "event_id") == 77
    s.close()


# ---- config and health -----------------------------------------------------------------------

def test_config_knobs_can_be_overridden():
    s = open_store(batch_ms=10, queue_limit=100, batch_rows=7, trim_interval=1.0, flush_interval=2.0,
                   flush_tick=0.5, persist_slots=3)
    c = s.config
    assert (c.batch_ms, c.queue_limit, c.batch_rows, c.trim_interval) == (10, 100, 7, 1.0)
    assert (c.flush_interval, c.flush_tick, c.persist_slots) == (2.0, 0.5, 3)
    s.close()


def test_health_reports_path_and_defaults():
    s = open_store()
    h = s.health()
    assert (h.store_ok, h.dropped_events, h.queue_depth, h.path) == (True, 0, 0, ":memory:")
    s.close()
