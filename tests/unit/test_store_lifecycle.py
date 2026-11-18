import asyncio

import pytest

from chimericmesh.diagnostics import Incident, IncidentTracker
from chimericmesh.events import Event
from chimericmesh.store.incidents import IncidentLifecycle, get_incident_row, load_incidents
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_incident(iid="abc1234567", **kw) -> Incident:
    base = dict(id=iid, host="db01", category="disk", signature="no space", severity=3, count=5,
                first_seen=10.0, last_seen=20.0, sources=["syslog"], sample="No space left on device",
                opened=True)
    base.update(kw)
    return Incident(**base)


def make_lc(store=None, clock=None, announced=None, **kw):
    clock = clock or Clock()
    said: list[str] = []

    async def announce(text: str) -> None:
        said.append(text)

    lc = IncidentLifecycle(IncidentTracker(), store=store, announce=announce, now=clock, reopen_after=300,
                           announced_getter=(announced or (lambda _id: None)), **kw)
    return lc, clock, said


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "lc.db", registry=core_registry())
    yield s
    s.close()


def stored(store, iid):
    return store.read_sync(lambda tx: get_incident_row(tx, iid), allow_blocking=True)


class HangingStore:
    """transact never returns; health says ok."""

    def __init__(self):
        self.calls = 0

    def health(self):
        class H:
            store_ok = True
        return H()

    async def transact(self, fn):
        self.calls += 1
        await asyncio.get_running_loop().create_future()


class GatedStore:
    def __init__(self, real):
        self.real, self.gate = real, asyncio.Event()

    def health(self):
        return self.real.health()

    async def transact(self, fn):
        await self.gate.wait()
        return await self.real.transact(fn)


class FailingStore:
    def health(self):
        class H:
            store_ok = True
        return H()

    async def transact(self, fn):
        raise OSError("disk gone")


def test_persist_is_a_noop_without_a_store():
    lc, _, _ = make_lc(None)
    asyncio.run(lc.persist(make_incident()))
    assert lc._dirty == {}


def test_persist_writes_the_row(store):
    lc, _, _ = make_lc(store)
    asyncio.run(lc.persist(make_incident()))
    assert stored(store, "abc1234567").count == 5


def test_maybe_flush_writes_at_most_once_per_interval(store):
    lc, clock, _ = make_lc(store)
    inc = make_incident()

    async def go():
        await lc.maybe_flush(inc)
        inc.count = 6
        clock.t += 10
        await lc.maybe_flush(inc)
        assert stored(store, inc.id).count == 5
        clock.t += 21
        await lc.maybe_flush(inc)
        assert stored(store, inc.id).count == 6

    asyncio.run(go())


def test_flush_dirty_writes_due_incident_without_new_events(store):
    lc, clock, _ = make_lc(store)
    inc = make_incident(count=1)

    async def go():
        await lc.persist(inc)
        inc.count = 5
        lc.mark_dirty(inc)
        await lc.flush_dirty()
        assert stored(store, inc.id).count == 1  # written 0 s ago: not due
        clock.t += 31
        await lc.flush_dirty()

    asyncio.run(go())
    assert stored(store, inc.id).count == 5
    assert lc._dirty == {}


def test_flush_due_lists_only_old_dirty_rows(store):
    lc, clock, _ = make_lc(store)
    a, b = make_incident("a"), make_incident("b")

    async def go():
        await lc.persist(a)
        lc.mark_dirty(a)
        lc.mark_dirty(b)  # never written: due now
        assert [r.id for r in lc.flush_due()] == ["b"]
        clock.t += 30
        assert sorted(r.id for r in lc.flush_due()) == ["a", "b"]

    asyncio.run(go())


def test_flush_all_writes_everything_and_stops_at_the_deadline(store):
    import time
    lc, _, _ = make_lc(store)
    incs = [make_incident(f"i{n}") for n in range(3)]
    for i in incs:
        lc.mark_dirty(i)
    asyncio.run(lc.flush_all(time.monotonic() - 1))  # deadline already passed
    assert len(lc._dirty) == 3 and store.read_sync(lambda tx: load_incidents(tx, 10)) == []
    asyncio.run(lc.flush_all(time.monotonic() + 5))
    assert lc._dirty == {} and len(store.read_sync(lambda tx: load_incidents(tx, 10))) == 3


def test_failing_store_leaves_incident_dirty_and_does_not_raise():
    lc, _, _ = make_lc(FailingStore())
    inc = make_incident()
    asyncio.run(lc.persist(inc))
    assert inc.id in lc._dirty


def test_snapshot_is_taken_before_the_write(store):
    gated = None

    async def go():
        nonlocal gated
        gated = GatedStore(store)
        lc, _, _ = make_lc(gated)
        inc = make_incident(count=5, sources=["syslog"])
        task = asyncio.ensure_future(lc.persist(inc))
        await asyncio.sleep(0)
        inc.count = 99
        inc.sources.append("irc")
        gated.gate.set()
        await task

    asyncio.run(go())
    row = stored(store, "abc1234567")
    assert row.count == 5 and row.sources == ("syslog",)


def test_5000_persists_on_a_stalled_store_leave_at_most_256_pending():
    async def go():
        hanging = HangingStore()
        lc, _, _ = make_lc(hanging)
        tasks = [asyncio.ensure_future(lc.persist(make_incident(f"i{n}"))) for n in range(5000)]
        await asyncio.sleep(0.05)
        pending = [t for t in tasks if not t.done()]
        assert len(pending) <= 256 and hanging.calls <= 256
        assert len(lc._dirty) >= 5000 - 256
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(go())


def test_marker_survives_a_skipped_persist(store):
    marker = {"v": None}

    async def go():
        gated = GatedStore(store)
        lc, clock, _ = make_lc(gated, persist_slots=1, announced=lambda _id: marker["v"])
        inc = make_incident()
        first = asyncio.ensure_future(lc.persist(inc))
        await asyncio.sleep(0)
        marker["v"] = 1  # the hub announced meanwhile
        await lc.persist(inc)  # slot busy: skipped, stays dirty
        assert inc.id in lc._dirty
        gated.gate.set()
        await first
        assert inc.id in lc._dirty  # the skipped persist re-marked it
        clock.t += 31
        await lc.flush_dirty()

    asyncio.run(go())
    assert stored(store, "abc1234567").announced_count == 1


# ---- reopen (task 8.3) -------------------------------------------------------------------------

from chimericmesh.store.audit import AuditLog  # noqa: E402
import chimericmesh.store.incidents as incidents_mod  # noqa: E402


def ev(msg="No space left on device", sev=3, ts=500.0, source="syslog", service=None):
    return Event(source, "db01", "app", sev, msg, msg, ts, service_id=service)


def resolved_incident(lc, ts=100.0, resolved_at=1000.0, count=7):
    inc = lc.tracker.add(ev(ts=ts))
    inc.count, inc.resolved, inc.resolved_at = count, True, resolved_at
    return inc


def audit_rows(store):
    return asyncio.run(store.audit.read(50))


def test_reopen_due_boundaries():
    lc, clock, _ = make_lc(None)
    inc = resolved_incident(lc, resolved_at=1000.0)
    clock.t = 1299.9
    assert not lc.reopen_due(inc)
    clock.t = 1300.0  # exactly reopen_after
    assert lc.reopen_due(inc)
    lc.reopen_after = 0
    clock.t = 1000.0
    assert lc.reopen_due(inc)
    open_inc = make_incident("x")
    assert not lc.reopen_due(open_inc)


def test_before_the_window_the_count_grows_and_opening_stays():
    lc, clock, said = make_lc(None)
    inc = resolved_incident(lc, resolved_at=1000.0, count=7)
    clock.t = 1100
    assert not lc.reopen_due(inc)
    lc.tracker.add(ev(ts=600.0))
    assert inc.count == 8 and inc.opening == 1 and inc.resolved and said == []


def test_reopen_resets_announces_once_and_is_visible_to_the_tracker(store):
    lc, clock, said = make_lc(store, announced=lambda _id: 1)
    inc = resolved_incident(lc)
    lc.tracker.reopen(inc, ev(sev=3, ts=900.0))
    asyncio.run(lc.reopen(inc))
    assert said == [f"REOPENED {inc.id}: disk on db01"]
    assert lc.tracker.get(inc.id) is inc and inc.count == 1 and inc.severity == 3 and inc.opening == 2
    row = stored(store, inc.id)
    assert (row.opening, row.count, row.resolved, row.resolved_at, row.announced_count) == (2, 1, False, None, 1)


def test_persist_happens_before_announce(store):
    lc, _, _ = make_lc(store)
    seen = []

    async def announce(text):
        seen.append(stored(store, inc.id))

    lc._announce = announce
    inc = resolved_incident(lc)
    lc.tracker.reopen(inc, ev(ts=900.0))
    asyncio.run(lc.reopen(inc))
    assert seen[0] is not None and seen[0].opening == 2


def test_reopen_audit_row_is_in_the_same_transaction(store):
    lc, _, _ = make_lc(store, audit_getter=lambda: store.audit)
    inc = resolved_incident(lc)
    inc.service_id = "svc9"
    lc.tracker.reopen(inc, ev(ts=900.0))
    asyncio.run(lc.reopen(inc))
    [row] = audit_rows(store)
    assert (row["action"], row["subject"], row["service_id"]) == ("incident_reopened", inc.id, "svc9")
    assert "opening=2" in row["detail"] and "svc9" in row["detail"]
    # a failing upsert rolls the audit row back with it
    inc2 = lc.tracker.add(ev("connection refused by upstream", ts=5.0))
    inc2.resolved, inc2.resolved_at = True, 1000.0
    lc.tracker.reopen(inc2, ev("connection refused by upstream", ts=900.0))
    inc2.severity = 99  # violates the CHECK on the incident row
    asyncio.run(lc.reopen(inc2))
    assert len(audit_rows(store)) == 1
    assert inc2.id in lc._dirty


def test_reopen_audit_detail_has_no_sample_or_signature(store):
    lc, _, _ = make_lc(store, audit_getter=lambda: store.audit)
    inc = lc.tracker.add(ev("CANARY-TEXT-77 no space left", ts=1.0))
    inc.resolved, inc.resolved_at = True, 1000.0
    lc.tracker.reopen(inc, ev("CANARY-TEXT-77 no space left", ts=900.0))
    asyncio.run(lc.reopen(inc))
    [row] = audit_rows(store)
    assert "canary" not in row["detail"].lower() and "canary" not in (row["subject"] + row["action"]).lower()


def test_reopen_again_gives_opening_3_and_a_second_line(store):
    lc, _, said = make_lc(store)
    inc = resolved_incident(lc)
    for expected in (2, 3):
        lc.tracker.reopen(inc, ev(ts=900.0))
        asyncio.run(lc.reopen(inc))
        assert inc.opening == expected
        inc.resolved, inc.resolved_at = True, 1000.0
    assert len(said) == 2 and all(s.startswith("REOPENED") for s in said)


def test_reopen_with_no_store_uses_the_audit_getter_at_call_time():
    sink_rows = []

    class Sink:
        async def append(self, actor, action, subject, detail="", **refs):
            sink_rows.append((actor, action, subject, detail, refs))

    holder = {"sink": None}
    lc, _, said = make_lc(None, audit_getter=lambda: holder["sink"])
    inc = resolved_incident(lc)
    lc.tracker.reopen(inc, ev(ts=900.0))
    asyncio.run(lc.reopen(inc))
    assert sink_rows == [] and len(said) == 1  # no sink attached: no audit row
    holder["sink"] = Sink()  # attached after construction
    inc.resolved, inc.resolved_at = True, 1000.0
    lc.tracker.reopen(inc, ev(ts=900.0))
    asyncio.run(lc.reopen(inc))
    assert len(sink_rows) == 1 and sink_rows[0][1] == "incident_reopened" and len(said) == 2


def test_resolved_at_survives_a_restart(tmp_path):
    path = tmp_path / "r.db"
    s = Store.open(path, registry=core_registry())
    lc, _, _ = make_lc(s)
    inc = resolved_incident(lc, resolved_at=1234.5)
    asyncio.run(lc.persist(inc))
    s.close()
    s = Store.open(path, registry=core_registry())
    [row] = s.read_sync(lambda tx: load_incidents(tx, 10))
    s.close()
    assert row.resolved and row.resolved_at == 1234.5


def test_failing_store_still_announces_and_leaves_dirty():
    lc, _, said = make_lc(FailingStore())
    inc = resolved_incident(lc)
    lc.tracker.reopen(inc, ev(ts=900.0))
    asyncio.run(lc.reopen(inc))
    assert len(said) == 1 and inc.id in lc._dirty


def test_no_webhook_is_involved_in_a_reopen():
    assert "webhook" not in open(incidents_mod.__file__, encoding="utf-8").read().lower()
