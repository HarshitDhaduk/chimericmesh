"""Properties 9, 10 and 11: event row fidelity, retention bounds, events after an id."""
import asyncio
import math

from hypothesis import given, settings, strategies as st

from chimericmesh.events import Event
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.proposals import create_proposal
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

DAY = 86400.0
any_text = st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF, blacklist_categories=()),
                   max_size=200)
long_or_short = st.one_of(any_text, st.builds(lambda n, c: c * n, st.integers(4090, 4200),
                                              st.sampled_from(["a", "\ud800", "\x00", "é"])))


def clean(s: str, n: int | None = None) -> str:
    out = s.encode("utf-8", "replace").decode("utf-8")
    return out if n is None else out[:n]


def open_store(**cfg) -> Store:
    return Store.open(":memory:", registry=core_registry(), config=StoreConfig.from_env({}, **cfg))


# ---- property 9 --------------------------------------------------------------------------------

@settings(max_examples=40, deadline=None)
@given(long_or_short, long_or_short, any_text, any_text, any_text, st.integers(0, 7),
       st.floats(allow_nan=False, allow_infinity=False, min_value=0, max_value=4e9),
       st.one_of(st.none(), any_text), st.one_of(st.none(), any_text))
def test_property_9_event_row_fidelity_and_caps(message, raw, host, app, source, severity, ts, svc, ls):
    store = open_store()
    try:
        event = Event(source, host, app, severity, message, raw, ts, id=1, service_id=svc, log_source_id=ls)
        store.events.put(event)  # must not raise
        asyncio.run(store.events.barrier())
        (row,) = store.read_sync(lambda tx: [dict(r) for r in tx.all("SELECT * FROM events")])
        assert row["id"] == 1 and row["ts"] == ts and row["severity"] == severity
        assert row["message"] == clean(message, 4096) and row["raw"] == clean(raw, 4096)
        assert len(row["message"]) <= 4096 and len(row["raw"]) <= 4096
        assert (row["source"], row["host"], row["app"]) == (clean(source), clean(host), clean(app))
        assert row["service_id"] == (None if svc is None else clean(svc))
        assert row["log_source_id"] == (None if ls is None else clean(ls))
        assert store.events.dropped_events == 0
    finally:
        store.close()


# ---- property 10 -------------------------------------------------------------------------------

def drain_trim(store: Store) -> int:
    """Run one full trim on the writer thread through the same steps the loop uses. Returns steps."""
    events = store.events
    state = store._state

    def run(tx) -> int:
        events._start_trim()
        steps = 0
        while events._trim is not None:
            events._trim_step(state)
            steps += 1
            assert steps < 1000, "trim did not terminate"
        return steps

    return store.read_sync(run)


inserts = st.lists(
    st.tuples(st.floats(min_value=0, max_value=4e9, allow_nan=False),   # ts, independent of order
              st.floats(min_value=0, max_value=3 * DAY)),              # step of the ingested clock
    min_size=0, max_size=60)


@settings(max_examples=40, deadline=None)
@given(inserts, st.integers(1, 30), st.integers(1, 3), st.floats(min_value=0, max_value=6 * DAY))
def test_property_10_retention_bounds(rows, max_events, ttl_days, tail):
    start = 1_000_000.0
    clock = {"t": start}
    store = Store.open(":memory:", registry=core_registry(), now=lambda: clock["t"],
                       config=StoreConfig.from_env({}, max_events=max_events, event_ttl_days=ttl_days,
                                                   trim_interval=3600))
    try:
        store.transact_sync(lambda tx: create_proposal(
            tx, incident_id="i", action="a", details="", service_id=None, incident_opening=1, now=1.0))
        asyncio.run(store.audit.append("a", "x", "s"))
        store.transact_sync(lambda tx: tx.execute(
            "INSERT INTO incidents (id, host, category, signature, severity, count, first_seen, last_seen, "
            "sample) VALUES ('i1', 'h', 'c', 's', 3, 1, 1, 1, 'x')"))

        ingested = []
        t = start
        for ts, step in rows:
            t += step  # ingested is non-decreasing
            ingested.append((ts, t))

        def insert(tx):
            tx.executemany(
                "INSERT INTO events (id, ts, source, host, app, severity, message, ingested) "
                "VALUES (?, ?, 'api', 'h', 'a', 3, 'm', ?)",
                [(i + 1, ts, ing) for i, (ts, ing) in enumerate(ingested)])

        store.transact_sync(insert)
        clock["t"] = t + tail
        cutoff = clock["t"] - ttl_days * DAY
        fresh = [i + 1 for i, (_, ing) in enumerate(ingested) if not ing < cutoff]
        expected = fresh[-max_events:] if fresh else []

        drain_trim(store)

        left = store.read_sync(lambda tx: [(r["id"], r["ingested"]) for r in tx.all(
            "SELECT id, ingested FROM events ORDER BY id")])
        assert [i for i, _ in left] == expected
        assert len(left) <= max_events
        assert all(not math.isnan(x) and not x < cutoff for _, x in left)
        if left:  # rows removed by the size cap all have a lower id than every survivor
            removed_by_cap = [i for i in fresh if i not in {j for j, _ in left}]
            assert all(i < left[0][0] for i in removed_by_cap)
        tallies = store.read_sync(lambda tx: tuple(
            tx.one("SELECT COUNT(*) FROM " + name)[0] for name in ("incidents", "proposals", "audit_log")))
        assert tallies == (1, 1, 1)
    finally:
        store.close()


# ---- property 11 -------------------------------------------------------------------------------

@settings(max_examples=50, deadline=None)
@given(st.sets(st.integers(1, 200), max_size=40), st.integers(0, 210), st.integers(0, 50))
def test_property_11_events_after_an_id(ids, after_id, limit):
    store = open_store()
    try:
        store.transact_sync(lambda tx: tx.executemany(
            "INSERT INTO events (id, ts, source, host, app, severity, message, ingested) "
            "VALUES (?, 1, 'api', 'h', 'a', 3, 'm', 1)", [(i,) for i in ids]))
        got = [e.id for e in asyncio.run(store.events.after(after_id, limit))]
        assert got == sorted(i for i in ids if i > after_id)[:limit]
        assert all(i > after_id for i in got) and got == sorted(got) and len(got) <= limit
    finally:
        store.close()
