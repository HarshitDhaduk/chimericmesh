"""Task 6.2: ``OffsetStore`` and the row/state conversions, on a file store and a ``:memory:`` store."""
from __future__ import annotations

import asyncio
import logging

import pytest

from chimericmesh.connectors.offsets import OffsetRow, OffsetStore, resume_state, row_from_state
from chimericmesh.connectors.schema import connectors_registry
from chimericmesh.connectors.tailread import FileIdent, TailState
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store, StoreError

SHA = "b" * 64
IDENT = FileIdent(dev=2**64 - 1, ino=12345, head_len=100, head_sha=SHA)


@pytest.fixture(params=["file", "memory"])
def store(request, tmp_path):
    path = tmp_path / "o.db" if request.param == "file" else ":memory:"
    s = Store.open(path, registry=connectors_registry())
    yield s
    s.close()


def run(coro):
    return asyncio.run(coro)


def row(sid: str, offset: int = 10, discarding: bool = False) -> OffsetRow:
    return row_from_state(sid, TailState(ident=IDENT, offset=offset, discarding=discarding), 1.0)


def test_row_from_state():
    assert row_from_state("s", TailState(), 1.0) is None
    r = row("s", 42, True)
    assert r == OffsetRow("s", IDENT.dev, IDENT.ino, 100, SHA, 42, True, 1.0)


def test_flush_upserts_and_load(store):
    offsets = OffsetStore(store)

    async def main():
        await offsets.flush([row("a", 10), row("b", 20)])
        await offsets.flush([row("a", 99)])
        await offsets.flush([])
        return await offsets.load()

    loaded = run(main())
    assert set(loaded) == {"a", "b"}
    assert loaded["a"].offset == 99 and loaded["b"].offset == 20
    assert loaded["a"].dev == 2**64 - 1 and loaded["a"].ino == 12345 and loaded["a"].head_sha == SHA


def test_bad_rows_are_skipped_with_one_warning(store, caplog):
    store.transact_sync(lambda tx: tx.executemany(
        "INSERT INTO tail_offsets (log_source_id, dev, ino, head_len, head_sha, byte_offset, discarding, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        [("good", "1", "2", 0, SHA, 5), ("bad-dev", "x1", "2", 0, SHA, 5), ("bad-ino", "1", "-", 0, SHA, 5)]),
        allow_blocking=True)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
        loaded = run(OffsetStore(store).load())
    assert set(loaded) == {"good"}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_bad_sha_length_row_is_skipped():
    # The table CHECK rejects a wrong length, so test the parser directly on a fetched-like row.
    from chimericmesh.connectors import offsets as mod

    assert mod._parse_row(("s", "1", "2", 0, "c" * 10, 0, 0, 0.0)) is None  # noqa: SLF001
    assert mod._parse_row(("s", "1", "2", 0, "g" * 64, 0, 0, 0.0)) is None  # noqa: SLF001
    assert mod._parse_row(("s", "1", "2", 0, SHA, 0, 0, 0.0)) is not None  # noqa: SLF001


def test_resume_state():
    s = resume_state(row("a", 77, True))
    assert s.ident == IDENT and s.offset == 77 and s.discarding and s.attach == "end" and s.mtime_ns == 0
    assert resume_state(None) == TailState()


def test_prune(store):
    offsets = OffsetStore(store)

    async def main():
        await offsets.flush([row("a"), row("b"), row("c")])
        n = await offsets.prune(frozenset({"b"}))
        return n, await offsets.load()

    n, loaded = run(main())
    assert n == 2 and set(loaded) == {"b"}


def test_drop_in_tx_commits_and_rolls_back_with_the_caller(store):
    offsets = OffsetStore(store)
    run(offsets.flush([row("a"), row("b"), row("c")]))

    class Boom(Exception):
        pass

    def failing(tx):
        OffsetStore.drop_in_tx(tx, ["a", "b"])
        raise Boom

    with pytest.raises(Exception):
        store.transact_sync(failing, allow_blocking=True)
    assert set(run(offsets.load())) == {"a", "b", "c"}
    store.transact_sync(lambda tx: OffsetStore.drop_in_tx(tx, ["a", "b"]), allow_blocking=True)
    assert set(run(offsets.load())) == {"c"}


def test_store_without_table(tmp_path, caplog):
    s = Store.open(tmp_path / "s.db", registry=services_registry())
    try:
        offsets = OffsetStore(s)

        async def main():
            assert await offsets.available() is False
            await offsets.flush([row("a")])
            await offsets.flush([row("b")])
            assert await offsets.load() == {}
            assert await offsets.prune(frozenset()) == 0

        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            run(main())
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        s.transact_sync(lambda tx: OffsetStore.drop_in_tx(tx, ["a"]), allow_blocking=True)
    finally:
        s.close()


def test_available_true(store):
    assert run(OffsetStore(store).available()) is True


def test_flush_closure_rerun_leaves_one_row(store):
    """The store may re-run a transaction closure; the upsert is idempotent."""
    calls = []
    real = store.transact

    async def twice(fn):
        calls.append(1)
        await real(fn)
        return await real(fn)

    store.transact = twice  # type: ignore[method-assign]
    try:
        offsets = OffsetStore(store)
        run(offsets.flush([row("a", 5)]))
    finally:
        store.transact = real  # type: ignore[method-assign]
    assert calls
    count = store.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM tail_offsets")[0], allow_blocking=True)
    assert count == 1


def test_store_error_propagates(store):
    async def fail(fn):
        raise StoreError("disk on fire")

    offsets = OffsetStore(store)
    offsets._available = True  # noqa: SLF001  (skip the table check)
    store.transact = fail  # type: ignore[method-assign]
    with pytest.raises(StoreError):
        run(offsets.flush([row("a")]))


def test_columns_hold_numbers_and_a_hash_only(store):
    cols = [r[1] for r in store.read_sync(lambda tx: tx.all("PRAGMA table_info(tail_offsets)"), allow_blocking=True)]
    assert cols == ["log_source_id", "dev", "ino", "head_len", "head_sha", "byte_offset", "discarding", "updated_at"]
