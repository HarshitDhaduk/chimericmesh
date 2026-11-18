import itertools

import pytest

from chimericmesh.store.errors import InvalidTransition
from chimericmesh.store.models import Proposal
from chimericmesh.store.proposals import (
    PROPOSAL_STATUSES,
    HubAuthority,
    check_transition,
    is_reserved_decider,
)

ALLOWED_ANY = {("pending", "approved"), ("pending", "denied"), ("approved", "done")}
BASELINE_KEYS = {"id", "incident_id", "action", "details", "status", "created", "decided_by",
                 "decided_at", "result_note"}
EXTENDED_KEYS = {"decision_origin", "service_id", "incident_opening", "approved_by_rule", "kind", "files",
                 "estimated_lines", "verification", "files_changed", "lines_changed", "verification_result"}


def attempt(old, new, *, origin="user", by="alice", authority=None):
    check_transition(old, new, origin=origin, decided_by=by, authority=authority)


def test_statuses():
    assert PROPOSAL_STATUSES == ("pending", "approved", "denied", "done")


@pytest.mark.parametrize("authority", [None, HubAuthority()])
@pytest.mark.parametrize("old,new", list(itertools.product(PROPOSAL_STATUSES, repeat=2)))
def test_every_pair_with_and_without_authority(old, new, authority):
    origin = "hub" if authority else "user"
    if (old, new) in ALLOWED_ANY:
        attempt(old, new, origin=origin, authority=authority)
    else:
        with pytest.raises(InvalidTransition):
            attempt(old, new, origin=origin, authority=authority)


@pytest.mark.parametrize("by", ["rule-revoked:r1", "rule-expired:r7", "rule-suspended:r12"])
def test_reserved_approved_to_denied_accepted_with_authority_and_hub_origin(by):
    attempt("approved", "denied", origin="hub", by=by, authority=HubAuthority())


@pytest.mark.parametrize("by", ["rule-revoked:r1", "rule-expired:r7", "rule-suspended:r12"])
def test_reserved_approved_to_denied_rejected_without_authority(by):
    for origin in ("user", "hub"):
        with pytest.raises(InvalidTransition):
            attempt("approved", "denied", origin=origin, by=by)


@pytest.mark.parametrize("by", ["alice", "rule-other:r1", "rule-revoked:", "rule-revoked: x", "rule:r1",
                                "withdrawn:bob", "", "Rule-Revoked:r1"])
def test_approved_to_denied_with_authority_needs_a_reserved_decider(by):
    with pytest.raises(InvalidTransition):
        attempt("approved", "denied", origin="hub", by=by, authority=HubAuthority())


def test_approved_to_denied_with_authority_needs_hub_origin():
    with pytest.raises(InvalidTransition):
        attempt("approved", "denied", origin="user", by="rule-revoked:r1", authority=HubAuthority())


@pytest.mark.parametrize("by", ["ruleman", "expired1", "withdrawal", "rules", "ruler"])
def test_ordinary_names_are_not_reserved(by):
    assert not is_reserved_decider(by)


@pytest.mark.parametrize("by", ["rule-x", "rule:x", "withdrawn:x", "expired:x", "expired-x",
                                "RULE-X", "Withdrawn:bob", " rule:x"])
def test_reserved_prefixes(by):
    assert is_reserved_decider(by)


def test_unknown_status_is_rejected():
    with pytest.raises(InvalidTransition):
        attempt("pending", "weird")
    with pytest.raises(InvalidTransition):
        attempt("weird", "done")


def test_proposal_dicts():
    p = Proposal(id="p1", incident_id="abc", action="a")
    assert set(p.to_dict()) == BASELINE_KEYS
    assert set(p.to_full_dict()) == BASELINE_KEYS | EXTENDED_KEYS
    assert p.to_dict()["status"] == "pending"
    assert all(p.to_full_dict()[k] is None for k in EXTENDED_KEYS)
