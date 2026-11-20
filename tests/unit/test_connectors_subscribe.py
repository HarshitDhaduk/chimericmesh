"""Task 7.3: registry injection (formats, status provider, subscription) and offset cleanup."""
from __future__ import annotations

import asyncio
import threading

import pytest

from chimericmesh.connectors.offsets import OffsetRow, OffsetStore
from chimericmesh.events import Event
from chimericmesh.parsers.formats import FORMAT_NAMES
from chimericmesh.services.errors import SubscriberFailed, ValidationFailed
from chimericmesh.services.models import LogSourceFields, LogSourcePatch, ServiceFields

SHA = "0" * 64


class Rec:
    def __init__(self) -> None:
        self.calls: list[tuple[Event, str | None]] = []

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


async def put_offset(store, sid: str) -> None:
    await OffsetStore(store).flush([OffsetRow(sid, 1, 2, 0, SHA, 10, False, 1.0)])


async def offset_ids(store) -> set[str]:
    return set(await store.read(lambda tx: [r[0] for r in tx.all("SELECT log_source_id FROM tail_offsets")]))


def test_constructor_injects_formats_status_provider_and_subscription(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        reg = m.registry
        assert reg.known_formats == frozenset(FORMAT_NAMES)
        await make_service(reg)
        await reg.add_log_source("web", lf(a, "ncsa"), actor="t")
        with pytest.raises(ValidationFailed):
            await reg.add_log_source("web", lf(tmp_path / "b.log", "iis"), actor="t")
        # no tailer yet (not started): the provider answers nothing for the source
        assert reg.status_of("web").log_sources[0].runtime is None
        await m.start()
        rt = reg.status_of("web").log_sources[0].runtime
        assert rt is not None and rt.status in ("running", "stopped")
        await m.aclose()

    asyncio.run(main())


def test_after_commit_starts_a_tailer_for_a_new_source(tmp_path, conn_registry, make_manager):
    async def main():
        rec = Rec()
        m = make_manager(conn_registry, rec)
        await make_service(m.registry)
        await m.start()
        assert m.tailers == {}
        a = tmp_path / "a.log"
        a.write_text("")
        await m.registry.add_log_source("web", lf(a), actor="t")
        assert set(m.tailers) == {"ls1"}          # after_commit finished before add_log_source returned
        await m.aclose()

    asyncio.run(main())


def test_removed_source_row_goes_in_the_same_transaction(tmp_path, conn_registry, make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        for n in ("a", "b"):
            await m.registry.add_log_source("web", lf(tmp_path / f"{n}.log"), actor="t")
        await put_offset(conn_registry, "ls1")
        await put_offset(conn_registry, "ls2")
        await m.registry.remove_log_source("web", "ls1", actor="t")
        assert await offset_ids(conn_registry) == {"ls2"}
        # an edit that removes nothing deletes nothing
        await m.registry.update_log_source("web", "ls2", LogSourcePatch(host="h"), actor="t")
        assert await offset_ids(conn_registry) == {"ls2"}
        # a deleted service takes its rows with it
        await m.registry.delete_service("web", actor="t")
        assert await offset_ids(conn_registry) == set()

    asyncio.run(main())


def test_a_later_failing_subscriber_rolls_the_removal_back(tmp_path, conn_registry, make_manager):
    a = tmp_path / "a.log"
    a.write_text("")

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.start()
        await put_offset(conn_registry, "ls1")

        def bad(tx, change):
            raise RuntimeError("nope")

        m.registry.subscribe(on_service_changed=bad, on_service_deleted=bad, name="late-subscriber")
        with pytest.raises(SubscriberFailed) as exc:
            await m.registry.remove_log_source("web", "ls1", actor="t")
        assert exc.value.name == "late-subscriber"
        assert await offset_ids(conn_registry) == {"ls1"}
        assert m.registry.source("ls1") is not None
        assert set(m.tailers) == {"ls1"}
        with pytest.raises(SubscriberFailed):
            await m.registry.delete_service("web", actor="t")
        assert await offset_ids(conn_registry) == {"ls1"}
        await m.aclose()

    asyncio.run(main())


def test_drop_offsets_runs_on_the_writer_thread_and_touches_no_manager_state(tmp_path, conn_registry, make_manager,
                                                                              monkeypatch):
    seen: list[str] = []
    real = OffsetStore.drop_in_tx

    def spy(tx, ids):
        seen.append(threading.current_thread().name)
        return real(tx, ids)

    monkeypatch.setattr(OffsetStore, "drop_in_tx", staticmethod(spy))

    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(tmp_path / "a.log"), actor="t")
        before = (dict(m._states), dict(m._counters), dict(m._tailers))
        await m.registry.remove_log_source("web", "ls1", actor="t")
        assert seen and set(seen) == {"store-writer"}
        assert before == ({}, {}, {})

    asyncio.run(main())


def test_drop_offsets_without_the_table_returns_quietly(tmp_path, svc_store, make_manager):
    async def main():
        m = make_manager(svc_store, Rec())
        await make_service(m.registry)
        await m.registry.add_log_source("web", lf(tmp_path / "a.log"), actor="t")
        await m.registry.remove_log_source("web", "ls1", actor="t")      # must not raise
        await m.registry.add_log_source("web", lf(tmp_path / "b.log"), actor="t")
        await m.registry.delete_service("web", actor="t")

    asyncio.run(main())


def test_a_skipped_after_commit_is_repaired_by_reconcile(tmp_path, conn_registry, make_manager):
    async def main():
        m = make_manager(conn_registry, Rec())
        await make_service(m.registry)
        await m.start()
        real = m.reconcile
        calls = {"n": 0}

        async def failing():
            calls["n"] += 1
            raise RuntimeError("after_commit skipped")

        m.reconcile = failing                       # type: ignore[method-assign]
        a, b = tmp_path / "a.log", tmp_path / "b.log"
        a.write_text("")
        b.write_text("")
        await m.registry.add_log_source("web", lf(a), actor="t")
        await m.registry.add_log_source("web", lf(b), actor="t")
        assert calls["n"] == 2 and m.tailers == {}
        m.reconcile = real                          # type: ignore[method-assign]
        await m.reconcile()                         # the 30-second path
        assert set(m.tailers) == {s.id for s in m.registry.log_sources(enabled_only=True)} == {"ls1", "ls2"}
        await m.aclose()

    asyncio.run(main())
