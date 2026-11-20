"""Task 7.2: the manager's reconcile, the runtime lookup and the one-path-one-tailer rule."""
from __future__ import annotations

import asyncio
import time

from chimericmesh.events import Event
from chimericmesh.services.models import LogSourceFields, LogSourcePatch, ServiceFields


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


async def make_service(reg, sid="web", **kw):
    await reg.create_service(ServiceFields(id=sid, name=sid, **kw), actor="t")


def append(path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_nothing_runs_before_start_and_start_runs_enabled_sources(tmp_path, conn_registry, make_manager):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_text("")
    b.write_text("")

    async def main():
        rec = Rec()
        m = make_manager(conn_registry, rec)
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.registry.add_log_source("web", lf(b, enabled=False), actor="t")
        await m.reconcile()
        assert m.tailers == {}                      # not started yet
        assert m.runtime("ls1") is None             # nothing is known before start
        await m.start()
        assert set(m.tailers) == {"ls1"}
        assert m.runtime("ls2").status == "stopped"
        assert m.runtime("ls1").status in ("running", "stopped")
        assert m.runtime("nope") is None
        await m.aclose()

    asyncio.run(main())


def test_reconcile_is_idempotent(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        first = dict(m.tailers)
        tasks = {k: t.task for k, t in first.items()}
        await m.reconcile()
        await m.reconcile()
        assert m.tailers == first
        assert {k: t.task for k, t in m.tailers.items()} == tasks
        await m.aclose()

    asyncio.run(main())


def test_format_or_host_edit_restarts_with_same_offset_and_counters(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        rec = Rec()
        m = make_manager(conn_registry, rec)
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await asyncio.sleep(0.1)
        append(a, "one\ntwo\n")
        await until(lambda: len(rec.calls) == 2)
        old = m.tailers["ls1"]
        append(a, "three\n")                        # written around the edit
        await m.registry.update_log_source("web", "ls1", LogSourcePatch(host="box9"), actor="t")
        await until(lambda: m.tailers["ls1"] is not old)
        append(a, "four\n")
        await until(lambda: len(rec.calls) >= 4)
        await asyncio.sleep(0.15)
        assert rec.messages == ["one", "two", "three", "four"]
        assert m.runtime("ls1").lines_read == 4 and m.runtime("ls1").events_emitted == 4
        assert rec.calls[-1][0].host == "box9"
        await m.aclose()

    asyncio.run(main())


def test_disable_then_enable_attaches_at_the_end(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        rec = Rec()
        m = make_manager(conn_registry, rec)
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await asyncio.sleep(0.1)
        append(a, "before\n")
        await until(lambda: len(rec.calls) == 1)
        await m.registry.update_log_source("web", "ls1", LogSourcePatch(enabled=False), actor="t")
        await until(lambda: "ls1" not in m.tailers)
        rt = m.runtime("ls1")
        assert rt.status == "stopped" and rt.events_emitted == 1          # counters stay
        append(a, "while disabled\n")
        await m.registry.update_log_source("web", "ls1", LogSourcePatch(enabled=True), actor="t")
        await until(lambda: "ls1" in m.tailers)
        await asyncio.sleep(0.15)
        append(a, "after\n")
        await until(lambda: len(rec.calls) == 2)
        await asyncio.sleep(0.1)
        assert rec.messages == ["before", "after"]
        assert m.runtime("ls1").events_emitted == 2
        await m.aclose()

    asyncio.run(main())


def test_two_sources_with_one_path_run_one_tailer(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        rec = Rec()
        m = make_manager(conn_registry, rec)
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        assert set(m.tailers) == {"ls1"}
        rt = m.runtime("ls2")
        assert rt.status == "error" and rt.last_error == "path already tailed by ls1"
        await asyncio.sleep(0.1)
        append(a, "x\ny\n")
        await until(lambda: len(rec.calls) == 2)
        await asyncio.sleep(0.15)
        assert rec.messages == ["x", "y"]
        assert {sid for _, sid in rec.calls} == {"ls1"}
        await m.aclose()

    asyncio.run(main())


def test_removing_a_source_stops_it_and_forgets_counters(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        assert m.runtime("ls1") is not None
        await m.registry.remove_log_source("web", "ls1", actor="t")
        await until(lambda: "ls1" not in m.tailers)
        assert m.runtime("ls1") is None
        await m.aclose()

    asyncio.run(main())


def test_service_delete_stops_all_its_tailers_quickly(tmp_path, conn_registry, make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        for i in range(6):
            p = tmp_path / f"{i}.log"
            p.write_text("")
            await m.registry.add_log_source("web", lf(p), actor="t")
        await m.start()
        assert len(m.tailers) == 6
        t0 = time.monotonic()
        await m.registry.delete_service("web", actor="t")
        await until(lambda: not m.tailers)
        assert time.monotonic() - t0 < 2.0
        await m.aclose()

    asyncio.run(main())


def test_a_finished_tailer_task_is_restarted(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        old = m.tailers["ls1"].task
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        assert old.done()
        await m.reconcile()
        new = m.tailers["ls1"].task
        assert new is not old and not new.done()
        await m.aclose()

    asyncio.run(main())


def test_missing_and_unreadable_sources_stay_visible(tmp_path, conn_registry, make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(tmp_path / "absent.log"), actor="t")
        await m.start()
        await until(lambda: m.runtime("ls1").status == "missing")
        assert m.runtime("ls1").last_error
        await m.aclose()

    asyncio.run(main())


def test_runtime_is_a_dictionary_lookup(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()

        def boom(*a, **k):
            raise AssertionError("runtime touched the store")

        conn_registry.read = boom
        conn_registry.read_sync = boom
        conn_registry.transact = boom
        assert m.runtime("ls1") is not None
        del conn_registry.read, conn_registry.read_sync, conn_registry.transact
        await m.aclose()

    asyncio.run(main())


def test_after_aclose_nothing_runs_and_reconcile_starts_nothing(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await m.aclose()
        assert m.tailers == {}
        await m.registry.add_log_source("web", lf(tmp_path / "b.log"), actor="t")
        await m.reconcile()
        assert m.tailers == {}

    asyncio.run(main())


def test_without_the_offsets_table_the_manager_still_tails(tmp_path, svc_store, make_manager, caplog):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        rec = Rec()
        m = make_manager(svc_store, rec)
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        with caplog.at_level("WARNING", logger="chimericmesh.connectors"):
            await m.start()
        assert len([r for r in caplog.records if "tail_offsets" in r.getMessage()]) == 1
        await asyncio.sleep(0.1)
        append(a, "hello\n")
        await until(lambda: rec.calls)
        await m.aclose()

    asyncio.run(main())
