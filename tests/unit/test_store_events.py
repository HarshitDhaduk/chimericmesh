import asyncio
import logging
import threading
import time

import pytest

from chimericmesh.events import Event
from chimericmesh.store import events as events_mod
from chimericmesh.store import writer as writer_mod
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

WRITER = "store-writer"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(writer_mod, "RETRY_DELAYS", (0.0, 0.0, 0.0))


def open_store(path=":memory:", now=None, **cfg) -> Store:
    config = StoreConfig.from_env({}, **cfg)
    kwargs = {"now": now} if now else {}
    return Store.open(path, registry=core_registry(), config=config, **kwargs)


def mk(i: int, **kw) -> Event:
    fields = dict(source="syslog", host="h", app="app", severity=3, message=f"m{i}", raw=f"r{i}", ts=float(i), id=i)
    fields.update(kw)
    return Event(**fields)


def rows(store: Store) -> list[dict]:
    return store.read_sync(lambda tx: [dict(r) for r in tx.all("SELECT * FROM events ORDER BY id")])


def count(store: Store) -> int:
    return store.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0])


def counter(store: Store, name: str) -> int:
    return store.read_sync(lambda tx: tx.counter_get(name))


def barrier(store: Store) -> None:
    asyncio.run(store.events.barrier())


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Blocker:
    """Holds the writer thread inside a job until released."""

    def __init__(self, store: Store) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()

        def fn(tx):
            self.started.set()
            self.gate.wait(10)

        self.thread = threading.Thread(target=lambda: asyncio.run(store.transact(fn)), daemon=True)
        self.thread.start()
        assert self.started.wait(5)

    def release(self) -> None:
        self.gate.set()
        self.thread.join(5)


# ---- 7.1 ---------------------------------------------------------------------------------------

def test_no_writer_thread_until_the_first_row():
    s = open_store()
    try:
        assert not any(t.name == WRITER for t in threading.enumerate())
        s.events.put(mk(1))
        assert any(t.name == WRITER for t in threading.enumerate())
    finally:
        s.close()


def test_put_never_blocks_and_stores_nothing_until_the_writer_runs():
    s = open_store(batch_ms=60_000)
    try:
        t0 = time.monotonic()
        for i in range(1, 4):
            s.events.put(mk(i))
        assert time.monotonic() - t0 < 0.5
        assert count(s) == 0
        barrier(s)
        assert count(s) == 3
    finally:
        s.close()


def test_rows_are_written_in_batches_of_at_most_batch_rows(monkeypatch):
    sizes = []
    real = events_mod._insert_batch

    def spy(tx, batch, deltas, last_id):
        sizes.append(len(batch))
        return real(tx, batch, deltas, last_id)

    monkeypatch.setattr(events_mod, "_insert_batch", spy)
    s = open_store(batch_rows=5, batch_ms=60_000)
    try:
        for i in range(1, 13):
            s.events.put(mk(i))
        assert wait_for(lambda: count(s) == 10)  # two full batches go out without waiting for the timer
        assert sizes == [5, 5]
        barrier(s)
        assert sizes == [5, 5, 2] and count(s) == 12
    finally:
        s.close()


def test_a_partial_batch_is_written_after_batch_ms():
    s = open_store(batch_rows=500, batch_ms=100)
    try:
        for i in range(1, 4):
            s.events.put(mk(i))
        assert wait_for(lambda: count(s) == 3, timeout=3)
    finally:
        s.close()


def test_stored_row_equals_the_event_plus_ingested(fake_clock):
    s = open_store(now=fake_clock)
    try:
        e = mk(7, source="telnet", host="web-1", app="nginx", severity=4, message="boom", raw="RAW",
               ts=123.5, service_id="svc1", log_source_id="ls1")
        s.events.put(e)
        fake_clock.advance(9)
        barrier(s)
        row = rows(s)[0]
        assert row == {"id": 7, "ts": 123.5, "source": "telnet", "host": "web-1", "app": "nginx",
                       "severity": 4, "message": "boom", "raw": "RAW", "service_id": "svc1",
                       "log_source_id": "ls1", "ingested": 1_000_000.0}
    finally:
        s.close()


def test_message_and_raw_are_cut_at_4096():
    s = open_store()
    try:
        s.events.put(mk(1, message="m" * 5000, raw="r" * 9000))
        barrier(s)
        row = rows(s)[0]
        assert len(row["message"]) == 4096 and len(row["raw"]) == 4096
    finally:
        s.close()


def test_lone_surrogate_and_nul_do_not_raise():
    s = open_store()
    try:
        s.events.put(mk(1, message="a\ud800b", raw="x\x00y", host="h\udfff"))
        s.events.put(mk(2))
        barrier(s)
        got = rows(s)
        assert [r["id"] for r in got] == [1, 2]
        assert "\ud800" not in got[0]["message"] and got[0]["raw"] == "x\x00y"
    finally:
        s.close()


def test_event_without_an_id_gets_a_row_id():
    s = open_store()
    try:
        s.events.put(mk(0))
        barrier(s)
        assert len(rows(s)) == 1
    finally:
        s.close()


def test_queue_drops_the_oldest_rows_and_warns_once_a_minute(fake_clock, caplog):
    s = open_store(now=fake_clock, queue_limit=50, batch_ms=60_000)
    try:
        blocker = Blocker(s)
        with caplog.at_level(logging.WARNING, logger="chimericmesh.store"):
            for i in range(1, 121):
                s.events.put(mk(i))
            warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
            assert len(warnings) == 1
            assert s.events.dropped_events == 70 and s.health().dropped_events == 70
            assert s.health().queue_depth == 50
            fake_clock.advance(61)
            s.events.put(mk(121))
            warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
            assert len(warnings) == 2
        blocker.release()
        barrier(s)
        ids = [r["id"] for r in rows(s)]
        assert ids == list(range(72, 122))  # the newest 50 survive
        assert s.events.dropped_events == 71
    finally:
        s.close()


def test_counter_deltas_and_high_water_mark_are_written_with_the_batch():
    s = open_store()
    try:
        for i in (1, 2, 3):
            s.events.put(mk(i))
        for i in (4, 5):
            s.events.put(mk(i, source="Weird Source!"))
        barrier(s)
        assert counter(s, "events_total") == 5
        assert counter(s, "evsrc:syslog") == 3
        assert counter(s, "evsrc:weird_source_") == 2
        assert counter(s, "event_id") == 5
    finally:
        s.close()


def test_dropped_rows_still_count_as_ingested(fake_clock):
    s = open_store(now=fake_clock, queue_limit=10, batch_ms=60_000)
    try:
        blocker = Blocker(s)
        for i in range(1, 31):
            s.events.put(mk(i))
        blocker.release()
        barrier(s)
        assert count(s) == 10 and s.events.dropped_events == 20
        assert counter(s, "events_total") == 30
        assert counter(s, "evsrc:syslog") == 30
    finally:
        s.close()


def test_close_drains_pending_rows(tmp_path):
    path = tmp_path / "e.db"
    s = open_store(path, batch_ms=60_000)
    for i in range(1, 4):
        s.events.put(mk(i))
    s.close()
    t = open_store(path)
    try:
        assert [r["id"] for r in rows(t)] == [1, 2, 3]
        assert counter(t, "events_total") == 3
    finally:
        t.close()


def test_duplicate_ids_are_ignored_counted_and_the_batch_commits():
    s = open_store()
    try:
        for i in (1, 2, 2, 3):
            s.events.put(mk(i, message=f"msg{i}"))
        barrier(s)
        assert [r["id"] for r in rows(s)] == [1, 2, 3]
        assert s.events.dropped_events == 1
        assert counter(s, "events_total") == 4
    finally:
        s.close()


def test_put_after_close_never_raises_and_counts_a_drop():
    s = open_store()
    s.close()
    s.events.put(mk(1))
    s.events.put(mk(2))
    assert s.events.dropped_events == 2
    assert s.health().queue_depth == 0


def test_persisted_plus_dropped_equals_ingested():
    s = open_store(queue_limit=20, batch_rows=7, batch_ms=5)
    try:
        for i in range(1, 201):
            s.events.put(mk(i))
        barrier(s)
        assert count(s) + s.events.dropped_events == 200
    finally:
        s.close()


# ---- 7.2 retention and write failures ----------------------------------------------------------

import sqlite3  # noqa: E402

from chimericmesh.store.proposals import create_proposal  # noqa: E402
from chimericmesh.store.tx import Tx  # noqa: E402

DAY = 86400.0


def put_many(store: Store, first: int, last: int, **kw) -> None:
    for i in range(first, last + 1):
        store.events.put(mk(i, **kw))


def seed_rows(store: Store, n: int, ingested: float, first_id: int = 1) -> None:
    def fn(tx):
        tx.executemany(
            "INSERT INTO events (id, ts, source, host, app, severity, message, ingested) "
            "VALUES (?, 1, 'api', 'h', 'a', 3, 'm', ?)",
            [(first_id + i, ingested) for i in range(n)])

    store.transact_sync(fn)


def trim_idle(store: Store) -> bool:
    return store.events._trim is None


def test_size_cap_keeps_the_newest_rows_and_the_table_stays_bounded():
    s = open_store(max_events=1000, trim_interval=3600)
    try:
        for chunk in range(6):
            put_many(s, chunk * 500 + 1, chunk * 500 + 500)
            barrier(s)
            assert count(s) <= 1000 + 500 + 500  # max_events + 500 + one batch
        assert wait_for(lambda: count(s) <= 1000 and trim_idle(s))
        assert count(s) == 1000
        ids = [r["id"] for r in rows(s)]
        assert ids == list(range(2001, 3001))  # oldest means lowest id
    finally:
        s.close()


def test_ttl_removes_rows_older_than_the_ttl_by_ingested_time(fake_clock):
    s = open_store(now=fake_clock, event_ttl_days=1, trim_interval=0.05)
    try:
        put_many(s, 1, 20)
        barrier(s)
        fake_clock.advance(2 * DAY)
        put_many(s, 21, 25)
        barrier(s)
        assert wait_for(lambda: [r["id"] for r in rows(s)] == [21, 22, 23, 24, 25])
    finally:
        s.close()


def test_an_old_ts_with_a_fresh_ingested_time_is_kept(fake_clock):
    s = open_store(now=fake_clock, event_ttl_days=1, trim_interval=0.05)
    try:
        put_many(s, 1, 5, ts=1.0)  # a replay: ancient message time, ingested now
        barrier(s)
        time.sleep(0.4)
        assert count(s) == 5
    finally:
        s.close()


def test_ttl_loop_goes_past_the_first_chunk_of_5000(fake_clock):
    s = open_store(now=fake_clock, event_ttl_days=1, trim_interval=0.05)
    try:
        seed_rows(s, 12_000, fake_clock() - 3 * DAY)
        seed_rows(s, 3, fake_clock(), first_id=20_000)
        assert wait_for(lambda: count(s) == 3 and trim_idle(s), timeout=20)
    finally:
        s.close()


def test_trim_runs_by_itself_when_no_rows_arrive(fake_clock):
    s = open_store(now=fake_clock, event_ttl_days=1, trim_interval=0.1)
    try:
        seed_rows(s, 50, fake_clock() - 2 * DAY)
        s.events.put(mk(1000))  # starts the writer thread; nothing else is queued afterwards
        assert wait_for(lambda: count(s) <= 1, timeout=5)
    finally:
        s.close()


def test_trim_serves_a_queued_job_between_chunks(fake_clock, monkeypatch):
    order: list[str] = []
    real = events_mod._ttl_chunk
    first = threading.Event()

    def spy(tx, cutoff, n):
        deleted = real(tx, cutoff, n)
        if deleted:
            order.append("ttl")
            if not first.is_set():
                first.set()
                threading.Thread(target=lambda: s.transact_sync(lambda tx2: order.append("job")),
                                 daemon=True).start()
                time.sleep(0.4)  # keep the first chunk running until the job is queued behind it
        return deleted

    monkeypatch.setattr(events_mod, "_ttl_chunk", spy)
    s = open_store(now=fake_clock, event_ttl_days=1, trim_interval=0.05)
    try:
        seed_rows(s, 12_000, fake_clock() - 3 * DAY)
        assert wait_for(lambda: count(s) == 0 and trim_idle(s), timeout=20)
        assert order.count("ttl") == 3  # 5000 + 5000 + 2000
        assert "job" in order and order.index("job") < len(order) - 1  # served before the trim finished
    finally:
        s.close()

def test_trimming_leaves_incidents_proposals_and_audit_untouched(fake_clock):
    s = open_store(now=fake_clock, max_events=10, trim_interval=3600)
    try:
        s.transact_sync(lambda tx: create_proposal(
            tx, incident_id="i", action="a", details="", service_id=None, incident_opening=1, now=1.0))
        asyncio.run(s.audit.append("a", "x", "s"))
        s.transact_sync(lambda tx: tx.execute(
            "INSERT INTO incidents (id, host, category, signature, severity, count, first_seen, last_seen, "
            "sample) VALUES ('i1', 'h', 'c', 's', 3, 1, 1, 1, 'x')"))

        def tallies():
            return s.read_sync(lambda tx: tuple(
                tx.one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("incidents", "proposals", "audit_log")))

        before = tallies()
        put_many(s, 1, 1000)  # two batches; the trim starts after the second
        barrier(s)
        assert wait_for(lambda: count(s) <= 10 and trim_idle(s))
        assert tallies() == before == (1, 1, 1)
    finally:
        s.close()


def test_write_failure_retries_then_drops_the_batch_and_the_writer_keeps_running(monkeypatch, caplog):
    calls = []
    real = events_mod._insert_batch

    def failing(tx, batch, deltas, last_id):
        calls.append(len(batch))
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events_mod, "_insert_batch", failing)
    s = open_store()
    try:
        with caplog.at_level(logging.ERROR, logger="chimericmesh.store"):
            put_many(s, 1, 3)
            barrier(s)
        assert calls == [3, 3, 3, 3]  # one try and three retries
        assert count(s) == 0 and s.events.dropped_events == 3
        assert s.health().store_ok is False
        assert any("dropped a batch" in r.getMessage() for r in caplog.records)
        # the next successful batch makes the store healthy again and carries the put-back deltas
        monkeypatch.setattr(events_mod, "_insert_batch", real)
        put_many(s, 4, 4)
        barrier(s)
        assert s.health().store_ok is True
        assert [r["id"] for r in rows(s)] == [4]
        assert counter(s, "events_total") == 4  # 3 put back + 1
        assert counter(s, "evsrc:syslog") == 4
        assert s.events.dropped_events == 3
    finally:
        s.close()


def test_rows_and_counters_are_one_transaction(monkeypatch):
    def broken(self, name, delta):
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(Tx, "counter_add", broken)
    s = open_store()
    try:
        put_many(s, 1, 3)
        barrier(s)
        assert count(s) == 0  # the insert was rolled back with the failed counter write
        assert s.events.dropped_events == 3 and s.health().store_ok is False
    finally:
        s.close()


# ---- 7.3 queries and the id seed ---------------------------------------------------------------

def run_async(coro):
    return asyncio.run(coro)


def test_after_returns_greater_ids_ascending_up_to_the_limit():
    s = open_store()
    try:
        put_many(s, 1, 10)
        barrier(s)
        got = run_async(s.events.after(3, 4))
        assert [e.id for e in got] == [4, 5, 6, 7]
        assert [e.id for e in run_async(s.events.after(8, 100))] == [9, 10]
        assert run_async(s.events.after(10, 5)) == []
        assert [e.id for e in run_async(s.events.after(0, 100, before_id=4))] == [1, 2, 3]
    finally:
        s.close()


def test_after_returns_events_with_every_field():
    s = open_store()
    try:
        s.events.put(mk(5, source="telnet", host="web", app="nginx", severity=4, message="boom", raw="RAW",
                        ts=9.5, service_id="svc", log_source_id="ls"))
        barrier(s)
        (e,) = run_async(s.events.after(0, 10))
        assert isinstance(e, Event) and e.id == 5
        assert (e.source, e.host, e.app, e.severity, e.message, e.raw, e.ts, e.service_id, e.log_source_id) == (
            "telnet", "web", "nginx", 4, "boom", "RAW", 9.5, "svc", "ls")
    finally:
        s.close()


def test_after_filters_by_source_and_service_before_the_limit():
    s = open_store()
    try:
        for i in range(1, 13):
            s.events.put(mk(i, source="irc" if i % 2 else "syslog", service_id="a" if i % 3 == 0 else None))
        barrier(s)
        assert [e.id for e in run_async(s.events.after(0, 3, source="irc"))] == [1, 3, 5]
        assert [e.id for e in run_async(s.events.after(0, 100, service_id="a"))] == [3, 6, 9, 12]
        assert [e.id for e in run_async(s.events.after(0, 2, service_id="unassigned"))] == [1, 2]
        assert [e.id for e in run_async(s.events.after(0, 100, source="irc", service_id="a"))] == [3, 9]
    finally:
        s.close()


def test_after_on_an_empty_table_is_empty():
    s = open_store()
    try:
        assert run_async(s.events.after(0, 10)) == []
        assert run_async(s.events.newest(10)) == []
    finally:
        s.close()


def test_newest_returns_the_newest_n_ascending():
    s = open_store()
    try:
        put_many(s, 1, 10)
        barrier(s)
        assert [e.id for e in run_async(s.events.newest(3))] == [8, 9, 10]
        assert [e.id for e in run_async(s.events.newest(100))] == list(range(1, 11))
    finally:
        s.close()


def test_barrier_returns_only_after_earlier_rows_are_committed():
    s = open_store(batch_ms=60_000)
    try:
        put_many(s, 1, 40)
        barrier(s)
        assert count(s) == 40
        barrier(s)  # nothing pending: returns at once
    finally:
        s.close()


def test_l5_barrier_forces_an_immediate_flush():
    s = open_store(batch_ms=10_000)
    try:
        put_many(s, 1, 3)
        t0 = time.monotonic()
        barrier(s)
        assert time.monotonic() - t0 < 1.0
        assert count(s) == 3
    finally:
        s.close()


def test_max_id_is_the_larger_of_the_table_and_the_counter(tmp_path):
    s = open_store(tmp_path / "m.db")
    try:
        assert s.events.max_id_sync() == 0
        put_many(s, 1, 5)
        barrier(s)
        assert s.events.max_id_sync() == 5
        s.transact_sync(lambda tx: tx.counter_raise("event_id", 500))
        assert s.events.max_id_sync() == 500
        s.transact_sync(lambda tx: tx.execute("INSERT INTO events (id, ts, source, host, app, severity, "
                                              "message, ingested) VALUES (900, 1, 'a', 'h', 'a', 3, 'm', 1)"))
        assert s.events.max_id_sync() == 900
    finally:
        s.close()


def test_max_id_survives_retention_emptying_the_table(tmp_path):
    path = tmp_path / "m.db"
    s = open_store(path)
    put_many(s, 1, 8)
    barrier(s)
    s.transact_sync(lambda tx: tx.execute("DELETE FROM events"))
    s.close()
    t = open_store(path)
    try:
        assert count(t) == 0 and t.previous_shutdown_clean is True
        assert t.events.max_id_sync() == 8
    finally:
        t.close()


def test_max_id_adds_the_crash_margin_after_an_unclean_shutdown(tmp_path):
    path = tmp_path / "m.db"
    s = open_store(path, queue_limit=100, batch_rows=10)
    put_many(s, 1, 5)
    barrier(s)
    s._abandon()
    t = open_store(path, queue_limit=100, batch_rows=10)
    try:
        assert t.previous_shutdown_clean is False
        assert t.events.crash_margin == 2 * 100 + 10
        assert t.events.max_id_sync() == 5 + 210
    finally:
        t.close()


def test_l2_seed_is_above_every_id_handed_out_even_when_rows_drop_during_a_commit(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    gate = threading.Event()
    in_commit = threading.Event()
    real = events_mod._insert_batch
    first = [True]

    def blocking(tx, batch, deltas, last_id):
        if first[0]:
            first[0] = False
            in_commit.set()
            gate.wait(10)
        return real(tx, batch, deltas, last_id)

    monkeypatch.setattr(events_mod, "_insert_batch", blocking)
    s = open_store(path, queue_limit=100, batch_rows=10, batch_ms=60_000)
    put_many(s, 1, 10)  # one full batch goes out and blocks in its commit
    assert in_commit.wait(5)
    put_many(s, 11, 210)  # 200 more ids handed out while it is blocked: 100 drop, 100 stay queued
    assert s.events.dropped_events == 100
    threading.Timer(0.3, gate.set).start()
    s._abandon()
    monkeypatch.setattr(events_mod, "_insert_batch", real)
    t = open_store(path, queue_limit=100, batch_rows=10)
    try:
        assert t.previous_shutdown_clean is False
        assert t.events.max_id_sync() > 210
    finally:
        t.close()


def test_m4_max_id_sync_starts_the_writer_and_runs_on_it(monkeypatch):
    names = []
    real = Tx.one

    def spy(self, sql, params=()):
        names.append(threading.current_thread().name)
        return real(self, sql, params)

    s = open_store()
    try:
        assert not any(t.name == WRITER for t in threading.enumerate())
        monkeypatch.setattr(Tx, "one", spy)
        s.events.max_id_sync()
        assert names and set(names) == {WRITER}
        assert any(t.name == WRITER for t in threading.enumerate())
        assert threading.current_thread().name not in names
    finally:
        s.close()
