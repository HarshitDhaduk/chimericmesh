"""Property 19: write accounting. Every ingested event is either stored or counted as dropped."""
import asyncio
import sqlite3
from unittest import mock

from hypothesis import given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.store import events as events_mod
from chimericmesh.store import writer as writer_mod
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


@settings(max_examples=25, deadline=None)
@given(st.integers(1, 150), st.integers(1, 40), st.integers(1, 12), st.sets(st.integers(1, 150), max_size=6),
       st.integers(0, 3))
def test_property_19_persisted_plus_dropped_equals_ingested(n, queue_limit, batch_rows, failing_first_ids,
                                                            barriers):
    real = events_mod._insert_batch

    def flaky(tx, batch, deltas, last_id):
        if batch[0][0] in failing_first_ids:
            raise sqlite3.OperationalError("database is locked")
        return real(tx, batch, deltas, last_id)

    cfg = StoreConfig.from_env({}, queue_limit=queue_limit, batch_rows=batch_rows, batch_ms=5)
    with mock.patch.object(writer_mod, "RETRY_DELAYS", (0.0, 0.0, 0.0)), \
            mock.patch.object(events_mod, "_insert_batch", flaky):
        store = Store.open(":memory:", registry=core_registry(), config=cfg)
        try:
            for i in range(1, n + 1):
                store.events.put(Event("syslog", "h", "a", 3, f"m{i}", "", float(i), id=i))
                if barriers and i % max(1, n // (barriers + 1)) == 0:
                    asyncio.run(store.events.barrier())
            asyncio.run(store.events.barrier())
            stored = store.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0])
            assert stored + store.events.dropped_events == n
            assert store.health().dropped_events == store.events.dropped_events
        finally:
            store.close()
