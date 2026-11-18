"""Properties 6, 7 and 8: transition table, origin independence, round trip."""
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from chimericmesh.store.errors import InvalidTransition
from chimericmesh.store.proposals import (
    PROPOSAL_STATUSES,
    HubAuthority,
    check_transition,
    create_proposal,
    get_proposal,
    transition_proposal,
)
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

RULE_BY = re.compile(r"^rule-(revoked|expired|suspended):\S+$")
statuses = st.sampled_from(PROPOSAL_STATUSES)
people = st.one_of(
    st.text(max_size=80),
    st.builds(lambda p, s: p + s, st.sampled_from(["expired", "expired-", "rule-", "rule-revoked:",
                                                   "rule-expired:", "rule-suspended:", "withdrawn:"]),
              st.text(max_size=20)),
)
text = st.text(alphabet=st.characters(min_codepoint=1, max_codepoint=0x10FFFF, blacklist_categories=()),
               max_size=700)


@settings(max_examples=300, deadline=None)
@given(statuses, statuses, st.sampled_from(["user", "hub", "other"]), people, st.booleans())
def test_property_6_transition_table(old, new, origin, by, with_authority):
    authority = HubAuthority() if with_authority else None
    plain = (old, new) in {("pending", "approved"), ("pending", "denied"), ("approved", "done")}
    reserved = (old, new) == ("approved", "denied") and with_authority and origin == "hub" \
        and RULE_BY.fullmatch(by) is not None
    if plain or reserved:
        check_transition(old, new, origin=origin, decided_by=by, authority=authority)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(old, new, origin=origin, decided_by=by, authority=authority)


@settings(max_examples=40, deadline=None)
@given(statuses, statuses, people, st.booleans())
def test_property_6_rejected_transition_changes_no_row(old, new, by, with_authority):
    store = Store.open(":memory:", registry=core_registry())
    try:
        def setup(tx):
            p = create_proposal(tx, incident_id="i", action="a", details="", service_id=None,
                                incident_opening=1, now=1.0)
            tx.execute("UPDATE proposals SET status = ? WHERE id = ?", (old, p.id))
            return p.id

        pid = store.transact_sync(setup)
        before = store.read_sync(lambda tx: get_proposal(tx, pid))
        authority = HubAuthority() if with_authority else None
        try:
            store.transact_sync(lambda tx: transition_proposal(
                tx, pid, new, decided_by=by, origin="hub" if with_authority else "user",
                authority=authority, now=9.0))
        except InvalidTransition:
            assert store.read_sync(lambda tx: get_proposal(tx, pid)) == before
        else:
            assert store.read_sync(lambda tx: get_proposal(tx, pid)).status == new
    finally:
        store.close()


@settings(max_examples=60, deadline=None)
@given(people, st.sampled_from(["approved", "denied"]), st.booleans())
def test_property_7_origin_is_independent_of_decided_by(by, status, with_authority):
    store = Store.open(":memory:", registry=core_registry())
    try:
        pid = store.transact_sync(lambda tx: create_proposal(
            tx, incident_id="i", action="a", details="", service_id=None, incident_opening=1, now=1.0)).id
        authority = HubAuthority() if with_authority else None
        out = store.transact_sync(lambda tx: transition_proposal(
            tx, pid, status, decided_by=by, origin="user", authority=authority, now=2.0))
        assert out.decision_origin == ("hub" if with_authority else "user")
        stored = store.read_sync(lambda tx: get_proposal(tx, pid))
        assert stored.decision_origin == out.decision_origin
    finally:
        store.close()


@settings(max_examples=12, deadline=None)
@given(text, text, text, text, st.sampled_from(["approved", "denied"]), st.one_of(st.none(), st.integers(1, 9)))
def test_property_8_round_trip_and_caps(action, details, by, note, status, opening):
    def clean(s, n):
        return s.encode("utf-8", "replace").decode("utf-8")[:n]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.db"
        store = Store.open(path, registry=core_registry())
        try:
            pid = store.transact_sync(lambda tx: create_proposal(
                tx, incident_id="inc", action=action, details=details, service_id=None,
                incident_opening=opening, now=5.0)).id
            decided = store.transact_sync(lambda tx: transition_proposal(
                tx, pid, status, decided_by=by, origin="user", authority=None, now=6.0, result_note=note))
        finally:
            store.close()
        store = Store.open(path, registry=core_registry())
        try:
            got = store.read_sync(lambda tx: get_proposal(tx, pid))
        finally:
            store.close()
    assert got == decided
    assert got.id == pid and got.status == status and got.created == 5.0 and got.decided_at == 6.0
    assert got.action == clean(action, 200) and got.details == clean(details, 500)
    assert got.decided_by == clean(by, 60) and got.result_note == clean(note, 300)
    assert got.incident_opening == opening
