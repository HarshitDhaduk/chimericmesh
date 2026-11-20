"""Task 7.4: offset flushing, restart and crash behaviour, shutdown limits."""
from __future__ import annotations

import asyncio
import dataclasses
import gc
import logging
import threading
import time

from chimericmesh.connectors.offsets import OffsetRow, OffsetStore
from chimericmesh.events import Event
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.store import StoreError


class Rec:
    def __init__(self) -> None:
        self.calls: list[tuple[Event, str | None]] = []

    @property
    def messages(self) -> list[str]:
        return [e.message for e, _ in self.calls]

    async def __call__(self, event: Event, log_source_id: str | None = None) -> Event:
        self.calls.append((event, log_source_id))
        return event


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def lf(path, fmt="plain", **kw) -> LogSourceFields:
    return LogSourceFields(path=str(path), format=fmt, **kw)


async def make_service(reg, sid="web"):
    await reg.create_service(ServiceFields(id=sid, name=sid), actor="t")


def append(path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


async def offsets(store) -> dict[str, tuple[int, float]]:
    rows = await store.read(lambda tx: [tuple(r) for r in tx.all("SELECT log_source_id, byte_offset, updated_at FROM tail_offsets")])
    return {r[0]: (r[1], r[2]) for r in rows}


def crash(m) -> None:
    """Drop a manager the way a killed process would: nothing is flushed or stopped politely."""
    for t in (m._flush_task, m._reconcile_task, *[x.task for x in m.tailers.values()]):
        if t is not None:
            t.cancel()


def test_dirty_offsets_are_flushed_within_the_interval_and_not_again(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await asyncio.sleep(0.1)
        append(a, "one\ntwo\n")
        await asyncio.sleep(0.3)                       # 1.5 flush intervals
        written = await offsets(conn_registry)
        assert written["ls1"][0] == a.stat().st_size
        await asyncio.sleep(0.5)
        assert await offsets(conn_registry) == written    # nothing changed, nothing written
        await m.aclose()

    asyncio.run(main())


def test_graceful_restart_resumes_exactly(tmp_path, conn_registry, make_manager, conn_config):
    a = tmp_path / "a.log"
    a.write_text("")
    slow = dataclasses.replace(conn_config, flush_interval=60.0, reconcile_interval=60.0)

    async def main():
        rec1 = Rec()
        m1 = make_manager(conn_registry, rec1, config=slow)
        await make_service(m1.registry)
        await m1.registry.add_log_source("web", lf(a), actor="t")
        await m1.start()
        await asyncio.sleep(0.1)
        append(a, "".join(f"l{i}\n" for i in range(5)))
        await until(lambda: len(rec1.calls) == 5)
        await m1.aclose()
        assert (await offsets(conn_registry))["ls1"][0] == a.stat().st_size
        append(a, "l5\nl6\n")
        rec2 = Rec()
        m2 = make_manager(conn_registry, rec2, config=slow)
        await m2.start()
        await until(lambda: len(rec2.calls) == 2)
        await asyncio.sleep(0.15)
        assert rec2.messages == ["l5", "l6"]
        await m2.aclose()

    asyncio.run(main())


def test_a_failing_flush_keeps_ids_dirty_and_recovers(tmp_path, conn_registry, make_manager, caplog):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        real = m._offsets.flush
        state = {"fail": True, "calls": 0}

        async def flaky(rows):
            state["calls"] += 1
            if state["fail"]:
                raise StoreError("store is down")
            await real(rows)

        m._offsets.flush = flaky                       # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            await m.start()
            await asyncio.sleep(0.1)
            append(a, "x\n")
            await until(lambda: state["calls"] >= 3)
            assert await offsets(conn_registry) == {}
            state["fail"] = False
            await until(lambda: state["calls"] >= 4)
            await asyncio.sleep(0.1)
        assert (await offsets(conn_registry))["ls1"][0] == a.stat().st_size
        warnings = [r for r in caplog.records if "offsets" in r.getMessage() and r.levelno == logging.WARNING]
        assert len(warnings) == 1                      # one per window
        await m.aclose()

    asyncio.run(main())


def test_reconcile_loop_prunes_orphan_rows(tmp_path, conn_registry, make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await OffsetStore(conn_registry).flush([OffsetRow("ls99", 1, 2, 0, "0" * 64, 5, False, 1.0)])
        await m.start()
        await asyncio.sleep(0.8)                      # reconcile_interval is 0.5
        assert "ls99" not in await offsets(conn_registry)
        await m.aclose()

    asyncio.run(main())


def test_crash_repeats_only_lines_after_the_last_flush(tmp_path, conn_registry, make_manager, conn_config):
    """After a crash the lines since the last flush come again (at most flush_interval of them).

    The other side cannot be simulated here: events that were submitted but not yet written by the store's
    event queue can be missing after a real crash, a few hundred milliseconds at most (design O4).
    """
    a = tmp_path / "a.log"
    a.write_text("")
    slow = dataclasses.replace(conn_config, flush_interval=60.0, reconcile_interval=60.0)

    async def main():
        rec1 = Rec()
        m1 = make_manager(conn_registry, rec1, config=slow)
        await make_service(m1.registry)
        await m1.registry.add_log_source("web", lf(a), actor="t")
        await m1.start()
        await asyncio.sleep(0.1)
        append(a, "l0\nl1\nl2\n")
        await until(lambda: len(rec1.calls) == 3)
        await m1._flush()
        append(a, "l3\nl4\n")
        await until(lambda: len(rec1.calls) == 5)
        crash(m1)
        await asyncio.sleep(0.05)
        rec2 = Rec()
        m2 = make_manager(conn_registry, rec2, config=slow)
        await m2.start()
        await until(lambda: len(rec2.calls) == 2)
        await asyncio.sleep(0.15)
        assert rec2.messages == ["l3", "l4"]
        await m2.aclose()

    asyncio.run(main())


def test_aclose_returns_in_time_with_a_store_that_never_answers_and_busy_tailers(tmp_path, conn_registry,
                                                                                   make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        paths = []
        for i in range(20):
            p = tmp_path / f"{i}.log"
            p.write_text("")
            paths.append(p)
            await m.registry.add_log_source("web", lf(p), actor="t")
        await m.start()
        await asyncio.sleep(0.1)
        for p in paths:
            append(p, "".join(f"line {j}\n" for j in range(300)))
        await asyncio.sleep(0.05)

        async def never(*a, **k):
            await asyncio.Event().wait()

        conn_registry.transact = never
        t0 = time.monotonic()
        await m.aclose(timeout=0.6)
        assert time.monotonic() - t0 < 1.2
        del conn_registry.transact
        assert m.tailers == {}

    asyncio.run(main())


def test_aclose_twice_and_nothing_outlives_it(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")
    conn_registry.read_sync(lambda tx: tx.one("SELECT 1"), allow_blocking=True)     # start the store's own thread
    before_threads = threading.active_count()

    async def main():
        before = set(asyncio.all_tasks())
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await asyncio.sleep(0.1)
        await m.aclose()
        await m.aclose()
        gc.collect()
        assert set(asyncio.all_tasks()) - before == set()

    asyncio.run(main())
    gc.collect()
    assert threading.active_count() == before_threads
