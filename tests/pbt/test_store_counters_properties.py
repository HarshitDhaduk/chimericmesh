import asyncio
import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.store.migrations import apply_migrations
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store
from chimericmesh.store.tx import SubscriberError, Tx, run_callbacks


@settings(max_examples=100, deadline=None)
@given(st.integers(0, 10**6), st.integers(0, 10**6), st.integers(1, 5))
def test_property_2_next_id_formula(stored, floor, times):
    c = sqlite3.connect(":memory:", isolation_level=None)
    apply_migrations(c, core_registry())
    if stored:
        c.execute("INSERT INTO counters VALUES ('proposal', ?)", (stored,))
    c.execute("BEGIN IMMEDIATE")
    tx = Tx(c)
    first = tx.counter_next("proposal", floor=floor)
    assert first == max(stored, floor) + 1
    got = [first] + [tx.counter_next("proposal", floor=floor) for _ in range(times - 1)]
    assert got == list(range(first, first + times))  # strictly increasing, no reuse, even with a stale floor
    c.execute("COMMIT")
    c.close()


@settings(max_examples=40, deadline=None)
@given(st.lists(st.booleans(), min_size=1, max_size=6))
def test_property_21_atomicity_under_callback_failure(fail_flags):
    store = Store.open(":memory:", registry=core_registry())
    try:
        def make(i, fail):
            def cb(tx):
                tx.counter_add(f"cb{i}", 1)
                if fail:
                    raise RuntimeError(f"cb{i}")
            return (f"sub{i}", cb)

        callbacks = [make(i, f) for i, f in enumerate(fail_flags)]

        def fn(tx):
            tx.counter_add("outer", 1)
            run_callbacks(tx, callbacks)

        async def go():
            return await store.transact(fn)

        if any(fail_flags):
            first_bad = fail_flags.index(True)
            with pytest.raises(SubscriberError) as ei:
                asyncio.run(go())
            assert ei.value.name == f"sub{first_bad}"
            rows = store.read_sync(lambda tx: tx.all("SELECT name FROM counters WHERE name != 'clean_shutdown'"))
            assert rows == []  # nothing from the outer fn or any earlier callback survived
        else:
            asyncio.run(go())
            assert store.read_sync(lambda tx: tx.counter_get("outer")) == 1
            assert all(store.read_sync(lambda tx, i=i: tx.counter_get(f"cb{i}")) == 1 for i in range(len(fail_flags)))
    finally:
        store.close()
