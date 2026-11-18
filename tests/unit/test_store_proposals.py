import asyncio
import json

import pytest

from chimericmesh.store.errors import InvalidTransition, NotFound
from chimericmesh.store.proposals import (
    HubAuthority,
    create_proposal,
    get_proposal,
    list_proposals,
    load_all_proposals,
    transition_proposal,
)
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


def open_at(path) -> Store:
    return Store.open(path, registry=core_registry())


def run(coro):
    return asyncio.run(coro)


def make(store: Store, *, incident="inc1", action="fix it", details="d", service=None, opening=1, now=100.0,
         extra=None):
    return run(store.transact(lambda tx: create_proposal(
        tx, incident_id=incident, action=action, details=details, service_id=service,
        incident_opening=opening, now=now, extra=extra)))


def decide(store: Store, pid, status="approved", by="alice", *, authority=None, now=200.0, **kw):
    origin = "hub" if authority else "user"
    return run(store.transact(lambda tx: transition_proposal(
        tx, pid, status, decided_by=by, origin=origin, authority=authority, now=now, **kw)))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "p.db"


def test_ids_survive_restart_and_deleted_rows_are_not_reused(db):
    s = open_at(db)
    assert [make(s).id, make(s).id] == ["p1", "p2"]
    s.close()
    s = open_at(db)
    assert make(s).id == "p3"
    s.transact_sync(lambda tx: tx.execute("DELETE FROM proposals WHERE id = 'p3'"))
    s.close()
    s = open_at(db)
    assert make(s).id == "p4"
    s.close()


def test_counter_lowered_below_stored_maximum_still_gives_a_higher_id(db):
    s = open_at(db)
    for _ in range(3):
        make(s)
    s.transact_sync(lambda tx: tx.counter_set("proposal", 0))
    assert make(s).id == "p4"
    s.close()


def test_counter_above_stored_maximum_wins(db):
    s = open_at(db)
    make(s)
    s.transact_sync(lambda tx: tx.counter_set("proposal", 40))
    assert make(s).id == "p41"
    s.close()


def test_concurrent_creates_get_distinct_ids(db):
    s = open_at(db)
    try:
        async def go():
            return await asyncio.gather(*[s.transact(lambda tx: create_proposal(
                tx, incident_id="i", action="a", details="", service_id=None,
                incident_opening=1, now=1.0)) for _ in range(25)])

        ids = {p.id for p in run(go())}
        assert ids == {f"p{n}" for n in range(1, 26)}
    finally:
        s.close()


def test_concurrent_decisions_give_one_winner_and_one_invalid_transition(db):
    s = open_at(db)
    try:
        pid = make(s).id

        async def go():
            return await asyncio.gather(
                *[s.transact(lambda tx, by=by: transition_proposal(
                    tx, pid, "approved", decided_by=by, origin="user", authority=None, now=5.0))
                  for by in ("a", "b", "c")],
                return_exceptions=True)

        results = run(go())
        assert sum(1 for r in results if not isinstance(r, Exception)) == 1
        assert sum(1 for r in results if isinstance(r, InvalidTransition)) == 2
    finally:
        s.close()


def test_counter_and_insert_commit_together(db):
    s = open_at(db)
    try:
        def fn(tx):
            create_proposal(tx, incident_id="i", action="a", details="", service_id=None,
                            incident_opening=1, now=1.0)
            raise KeyError("after counter_next")

        with pytest.raises(KeyError):
            run(s.transact(fn))
        assert s.read_sync(lambda tx: tx.counter_get("proposal")) == 0
        assert s.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM proposals")[0]) == 0
        assert make(s).id == "p1"
    finally:
        s.close()


def test_field_caps(db):
    s = open_at(db)
    try:
        p = make(s, action="a" * 300, details="d" * 900)
        assert len(p.action) == 200 and len(p.details) == 500
        decide(s, p.id, "approved", by="b" * 100)
        done = decide(s, p.id, "done", result_note="n" * 900)
        got = s.read_sync(lambda tx: get_proposal(tx, p.id))
        assert len(got.decided_by) == 60 and len(got.result_note) == 300
        assert got == done
    finally:
        s.close()


def test_lone_surrogates_do_not_raise(db):
    s = open_at(db)
    try:
        p = make(s, action="a\ud800b", details="\udfff")
        assert "\ud800" not in p.action
        decide(s, p.id, "approved", by="x\ud800")
    finally:
        s.close()


def test_decision_origin_follows_authority_not_decided_by(db):
    s = open_at(db)
    try:
        a, b = make(s), make(s)
        assert decide(s, a.id, "approved", by="expired-bob").decision_origin == "user"
        assert decide(s, b.id, "approved", by="whoever", authority=HubAuthority()).decision_origin == "hub"
    finally:
        s.close()


def test_approved_to_denied_only_for_hub_rule_logic(db):
    s = open_at(db)
    try:
        a, b = make(s), make(s)
        decide(s, a.id)
        decide(s, b.id)
        with pytest.raises(InvalidTransition):
            decide(s, a.id, "denied", by="rule-revoked:r1")
        assert s.read_sync(lambda tx: get_proposal(tx, a.id)).status == "approved"
        out = decide(s, b.id, "denied", by="rule-revoked:r1", authority=HubAuthority())
        assert out.status == "denied" and out.decision_origin == "hub"
        for new in ("approved", "denied", "done", "pending"):
            with pytest.raises(InvalidTransition):
                decide(s, b.id, new, by="rule-revoked:r1", authority=HubAuthority())
    finally:
        s.close()


def test_approved_by_rule_needs_authority(db):
    s = open_at(db)
    try:
        p = make(s)
        with pytest.raises(InvalidTransition):
            decide(s, p.id, approved_by_rule="r1")
        assert s.read_sync(lambda tx: get_proposal(tx, p.id)).status == "pending"
        out = decide(s, p.id, authority=HubAuthority(), approved_by_rule="r1")
        assert out.approved_by_rule == "r1"
    finally:
        s.close()


def test_unknown_id_and_unknown_extra_key(db):
    s = open_at(db)
    try:
        with pytest.raises(NotFound):
            decide(s, "p99")
        with pytest.raises(ValueError):
            make(s, extra={"bogus": 1})
        assert s.read_sync(lambda tx: tx.counter_get("proposal")) == 0
        p = make(s)
        with pytest.raises(ValueError):
            decide(s, p.id, "approved", extra={"id": "p5"})
        assert s.read_sync(lambda tx: get_proposal(tx, p.id)).status == "pending"
    finally:
        s.close()


def test_extended_columns_are_stored_and_reloaded(db):
    s = open_at(db)
    p = make(s, service="svc", opening=3, extra={"kind": "code", "files": ["a.py", "b.py"],
                                                 "estimated_lines": 12, "verification": "pytest -q"})
    decide(s, p.id)
    done = decide(s, p.id, "done", result_note="ok", extra={
        "files_changed": ["a.py"], "lines_changed": 9, "verification_result": "passed"})
    s.close()
    s = open_at(db)
    got = s.read_sync(lambda tx: get_proposal(tx, p.id))
    assert got == done
    assert (got.service_id, got.incident_opening, got.kind) == ("svc", 3, "code")
    assert got.files == ["a.py", "b.py"] and got.files_changed == ["a.py"]
    assert (got.estimated_lines, got.lines_changed, got.verification_result) == (12, 9, "passed")
    raw = s.read_sync(lambda tx: tx.one("SELECT files FROM proposals WHERE id = ?", (p.id,))[0])
    assert json.loads(raw) == ["a.py", "b.py"]
    s.close()


def test_row_without_extended_columns_loads(db):
    s = open_at(db)
    s.transact_sync(lambda tx: tx.execute(
        "INSERT INTO proposals (id, seq, incident_id, action, status, created) "
        "VALUES ('p7', 7, 'i', 'a', 'pending', 1.0)"))
    got = s.read_sync(lambda tx: get_proposal(tx, "p7"))
    assert got.details == "" and got.decided_at is None and got.decision_origin is None
    assert got.files is None and got.service_id is None
    assert make(s).id == "p8"
    s.close()


def test_decided_at_and_by_are_set_on_decision_only(db):
    s = open_at(db)
    try:
        p = make(s, now=100.0)
        assert p.created == 100.0 and p.decided_at is None
        a = decide(s, p.id, by="alice", now=150.0)
        assert (a.decided_by, a.decided_at) == ("alice", 150.0)
        d = decide(s, p.id, "done", by="agent", now=170.0, result_note="fine")
        assert (d.decided_by, d.decided_at, d.result_note) == ("alice", 150.0, "fine")
    finally:
        s.close()


def test_list_filters_apply_before_the_limit(db):
    s = open_at(db)
    try:
        for i in range(10):
            make(s, service="a" if i % 3 == 0 else (None if i % 3 == 1 else "b"))
        # a: p1 p4 p7 p10 ; none: p2 p5 p8 ; b: p3 p6 p9
        ids = lambda **kw: [p.id for p in s.read_sync(lambda tx: list_proposals(tx, **kw))]  # noqa: E731
        assert ids(service_id="a", limit=2) == ["p7", "p10"]
        assert ids(service_id="unassigned") == ["p2", "p5", "p8"]
        assert ids(service_id="unassigned", limit=1) == ["p8"]
        assert ids(limit=3) == ["p8", "p9", "p10"]
        decide(s, "p1")
        decide(s, "p2")
        assert ids(status="approved") == ["p1", "p2"]
        assert ids(status="approved", service_id="a") == ["p1"]
        assert ids(status="pending", limit=2) == ["p9", "p10"]
        assert len(ids()) == 10
    finally:
        s.close()


def test_load_all_returns_everything_in_order(db):
    s = open_at(db)
    try:
        for _ in range(12):
            make(s)
        got = s.read_sync(load_all_proposals)
        assert [p.id for p in got] == [f"p{n}" for n in range(1, 13)]
    finally:
        s.close()
