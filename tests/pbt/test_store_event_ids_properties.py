"""Property 5: event ids strictly increase across trims, graceful restarts and crashes."""
import asyncio
import tempfile
from pathlib import Path

from hypothesis import example, given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

CFG = dict(queue_limit=50, batch_rows=10, batch_ms=5)

ops = st.lists(
    st.one_of(
        st.tuples(st.just("ingest"), st.integers(1, 15)),
        st.tuples(st.just("trim"), st.just(0)),
        st.tuples(st.just("restart"), st.just(0)),
        st.tuples(st.just("crash"), st.just(0)),
    ),
    min_size=1,
    max_size=8,
)


def open_store(path):
    return Store.open(path, registry=core_registry(), config=StoreConfig.from_env({}, **CFG))


async def run(path: Path, sequence):
    seen: list[int] = []
    store = open_store(path)
    hub = Hub(store=store)
    try:
        for op, n in sequence:
            if op == "ingest":
                for i in range(n):
                    e = await hub.ingest(Event("syslog", "h", "a", 6, f"m{i}", "", 1.0))
                    assert e.to_dict()["id"] == e.id
                    seen.append(e.id)
            elif op == "trim":
                await store.events.barrier()
                await store.transact(lambda tx: tx.execute("DELETE FROM events"))
            elif op == "restart":
                await hub.aclose()
                store.close()
                store = open_store(path)
                hub = Hub(store=store)
            else:  # crash: writer killed without draining or marking a clean shutdown
                store._abandon()
                store = open_store(path)
                hub = Hub(store=store)
        await store.events.barrier()
        rows = await store.events.after(0, 10_000)
        by_id = {e.id: e for e in rows}
        for e in hub.events:  # the ring and the stored row carry the same id
            if e.id in by_id:
                assert by_id[e.id].message == e.message
        await hub.aclose()
    finally:
        store.close()
    return seen


@settings(deadline=None)
@example([("crash", 0), ("ingest", 1), ("crash", 0), ("ingest", 1)])  # B2 regression
@given(ops)
def test_event_ids_strictly_increase(sequence):
    with tempfile.TemporaryDirectory() as d:
        seen = asyncio.run(run(Path(d) / "ids.db", sequence))
    assert all(a < b for a, b in zip(seen, seen[1:])), seen
