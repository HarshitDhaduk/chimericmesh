"""Properties 1, 3 and 4: proposal ids are never reused, have the right format, and stay distinct
under concurrency."""
import asyncio
import re
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from chimericmesh.store.proposals import create_proposal
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

ID_FORMAT = re.compile(r"^p[1-9][0-9]*$")

ops = st.lists(
    st.one_of(
        st.tuples(st.just("create"), st.integers(1, 4)),
        st.tuples(st.just("delete"), st.integers(0, 10)),    # delete the row at this rank (SQL)
        st.tuples(st.just("trim"), st.integers(0, 5)),       # delete the oldest n rows (SQL)
        st.tuples(st.just("restart"), st.just(0)),
    ),
    min_size=1, max_size=14,
)


def create_one(store: Store) -> str:
    return asyncio.run(store.transact(lambda tx: create_proposal(
        tx, incident_id="i", action="a", details="", service_id=None, incident_opening=1, now=1.0))).id


@settings(max_examples=15, deadline=None)
@given(ops)
def test_property_1_and_3_ids_are_never_reused_and_well_formed(operations):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.db"
        store = Store.open(path, registry=core_registry())
        issued: list[int] = []
        try:
            for op, arg in operations:
                if op == "create":
                    for _ in range(arg):
                        pid = create_one(store)
                        assert ID_FORMAT.fullmatch(pid)
                        issued.append(int(pid[1:]))
                elif op == "delete":
                    store.transact_sync(lambda tx: tx.execute(
                        "DELETE FROM proposals WHERE id IN (SELECT id FROM proposals ORDER BY seq "
                        "LIMIT 1 OFFSET ?)", (arg,)))
                elif op == "trim":
                    store.transact_sync(lambda tx: tx.execute(
                        "DELETE FROM proposals WHERE id IN (SELECT id FROM proposals ORDER BY seq LIMIT ?)",
                        (arg,)))
                else:
                    store.close()
                    store = Store.open(path, registry=core_registry())
        finally:
            store.close()
        assert len(set(issued)) == len(issued), "an id was issued twice"
        # never reused and, within a run, every new id is above every earlier one
        assert issued == sorted(issued)


@settings(max_examples=10, deadline=None)
@given(st.integers(0, 6), st.integers(1, 20))
def test_property_4_concurrent_creation_is_distinct_and_consecutive(n0, k):
    store = Store.open(":memory:", registry=core_registry())
    try:
        for _ in range(n0):
            create_one(store)

        async def go():
            return await asyncio.gather(*[store.transact(lambda tx: create_proposal(
                tx, incident_id="i", action="a", details="", service_id=None,
                incident_opening=1, now=1.0)) for _ in range(k)])

        ids = [p.id for p in asyncio.run(go())]
        assert len(set(ids)) == k
        assert set(ids) == {f"p{n}" for n in range(n0 + 1, n0 + k + 1)}
    finally:
        store.close()
