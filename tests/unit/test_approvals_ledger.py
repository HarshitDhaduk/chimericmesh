"""Usage ledger (hour window, per-opening counts, the single cooldown formula) and the proposal limits.

No test here uses ``now - completed_at``: the cooldown is ``reopen_at(opening) - completed_at < cooldown`` (H1).
"""
from __future__ import annotations

import pytest

from chimericmesh.approvals.errors import ActiveProposalExists, ProposalRateLimited
from chimericmesh.approvals.ledger import OpeningRow, UsageLedger, UsageRow
from chimericmesh.approvals.limits import MAX_PROPOSALS_PER_SERVICE_HOUR, ProposalLimits

H = 3600.0
COOLDOWN = 86400.0


def test_hour_window_counts_reservations_and_drops_at_exactly_3600():
    ledger = UsageLedger()
    ledger.reserve("r1", "i1", 1, now=0.0)
    assert ledger.usage("r1", "i1", 1, 3599.9).hour == 1
    assert ledger.usage("r1", "i1", 1, 3600.0).hour == 0
    assert ledger.usage("r2", "i1", 1, 0.0).hour == 0


def test_reserve_then_release_restores_the_count():
    ledger = UsageLedger()
    token = ledger.reserve("r1", "i1", 1, now=10.0)
    assert ledger.usage("r1", "i1", 1, 11.0).hour == 1
    ledger.release(token)
    assert ledger.usage("r1", "i1", 1, 11.0) == ledger.usage("r9", "i9", 1, 11.0)
    ledger.release(token)  # idempotent


def test_per_opening_count_is_keyed_by_incident_and_opening():
    ledger = UsageLedger()
    ledger.reserve("r1", "i1", 1, now=0.0)
    assert ledger.usage("r1", "i1", 1, 5.0).incident_opening == 1
    assert ledger.usage("r1", "i1", 2, 5.0).incident_opening == 0
    assert ledger.usage("r1", "i2", 1, 5.0).incident_opening == 0
    assert ledger.usage("r2", "i1", 1, 5.0).incident_opening == 0


def test_opening_count_outlives_the_hour_window():
    ledger = UsageLedger()
    ledger.reserve("r1", "i1", 1, now=0.0)
    usage = ledger.usage("r1", "i1", 1, 10 * H)
    assert usage.hour == 0 and usage.incident_opening == 1


def test_bind_and_completed_and_approved_at():
    ledger = UsageLedger()
    token = ledger.reserve("r1", "i1", 1, now=7.0)
    ledger.bind(token, "p1")
    assert ledger.approved_at("p1") == 7.0
    assert ledger.approved_at("p9") is None
    ledger.completed("p1", 99.0)
    ledger.completed("p9", 1.0)  # unknown: ignored


def test_note_opening_first_sighting_wins():
    ledger = UsageLedger()
    assert ledger.note_opening("i1", 2, 100.0) is True
    assert ledger.note_opening("i1", 2, 500.0) is False
    assert ledger.reopen_at("i1", 2) == 100.0
    assert ledger.reopen_at("i1", 3) is None


def _completed(ledger: UsageLedger, incident="i1", opening=1, at=0.0, pid="p1"):
    token = ledger.reserve("r1", incident, opening, now=at - 60)
    ledger.bind(token, pid)
    ledger.completed(pid, at)


def test_cooldown_reopen_one_hour_after_completion_applies_whenever_asked():
    ledger = UsageLedger()
    _completed(ledger, at=0.0)
    ledger.note_opening("i1", 2, 1 * H)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"
    # asked again after the clock moved to 30 hours: the answer must not change
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"


def test_cooldown_does_not_apply_when_reopen_is_after_the_window():
    ledger = UsageLedger()
    _completed(ledger, at=0.0)
    ledger.note_opening("i1", 2, 25 * H)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) is None


def test_cooldown_exactly_at_the_limit_does_not_apply():
    ledger = UsageLedger()
    _completed(ledger, at=0.0)
    ledger.note_opening("i1", 2, COOLDOWN)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) is None


def test_completion_recorded_after_the_reopen_gives_the_id():
    ledger = UsageLedger()
    token = ledger.reserve("r1", "i1", 1, now=0.0)
    ledger.bind(token, "p1")
    ledger.note_opening("i1", 2, 5 * H)
    ledger.completed("p1", 6 * H)  # negative difference stays below the cooldown
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"


def test_third_opening_far_later_is_not_in_cooldown():
    ledger = UsageLedger()
    _completed(ledger, at=0.0)
    ledger.note_opening("i1", 2, 1 * H)
    ledger.note_opening("i1", 3, 101 * H)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"
    assert ledger.cooldown_for("i1", 3, COOLDOWN) is None


def test_cooldown_needs_a_completed_use_and_a_later_opening():
    ledger = UsageLedger()
    assert ledger.cooldown_for("i1", 2, COOLDOWN) is None          # no use at all
    token = ledger.reserve("r1", "i1", 1, now=0.0)
    ledger.bind(token, "p1")                                       # a use without completed_at
    ledger.note_opening("i1", 2, 1.0)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) is None
    ledger.completed("p1", 0.5)
    assert ledger.cooldown_for("i1", 1, COOLDOWN) is None          # opening 1 has no earlier use
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"
    assert ledger.cooldown_for("i1", 2, 0.0) is None               # cooldown 0 never applies
    assert ledger.cooldown_for("i2", 2, COOLDOWN) is None          # another incident


def test_cooldown_without_a_sighting_is_none():
    ledger = UsageLedger()
    _completed(ledger, at=0.0)
    assert ledger.cooldown_for("i1", 2, COOLDOWN) is None


def test_latest_completed_use_is_returned():
    ledger = UsageLedger()
    _completed(ledger, opening=1, at=100.0, pid="p1")
    _completed(ledger, opening=2, at=200.0, pid="p2")
    ledger.note_opening("i1", 3, 300.0)
    assert ledger.cooldown_for("i1", 3, COOLDOWN) == "p2"


def test_seed_and_prune():
    ledger = UsageLedger()
    now = 40 * 86400.0
    ledger.seed([UsageRow("r1", "p1", "i1", 1, "svc", now - 100, now - 50),
                 UsageRow("r1", "p0", "i0", 1, "svc", now - 32 * 86400, None)],
                [OpeningRow("i1", 2, now - 20), OpeningRow("i0", 2, now - 32 * 86400)])
    assert ledger.usage("r1", "i1", 1, now).hour == 1
    assert ledger.reopen_at("i1", 2) == now - 20
    ledger.prune(now)
    assert ledger.reopen_at("i0", 2) is None
    assert ledger.approved_at("p0") is None and ledger.approved_at("p1") == now - 100
    assert ledger.cooldown_for("i1", 2, COOLDOWN) == "p1"


# ---- proposal limits -------------------------------------------------------------------------------------

def none_active(_incident: str):
    return None


def test_limits_off_checks_and_reserves_nothing():
    limits = ProposalLimits()
    for _ in range(50):
        assert limits.reserve("i1", "svc", 0.0, enforce=False, active_of=lambda i: "p1") == 0
    assert limits.count("svc", 0.0) == 0


def test_active_proposal_blocks_a_second_one():
    limits = ProposalLimits()
    with pytest.raises(ActiveProposalExists) as err:
        limits.reserve("i1", "svc", 0.0, enforce=True, active_of=lambda i: "p7")
    assert err.value.proposal_id == "p7"


def test_in_flight_reservation_blocks_the_same_incident_until_released():
    limits = ProposalLimits()
    token = limits.reserve("i1", "svc", 0.0, enforce=True, active_of=none_active)
    with pytest.raises(ActiveProposalExists):
        limits.reserve("i1", "svc", 0.0, enforce=True, active_of=none_active)
    limits.release(token)
    limits.reserve("i1", "svc", 0.0, enforce=True, active_of=none_active)


def test_commit_frees_the_incident_but_keeps_the_hour_slot():
    limits = ProposalLimits()
    token = limits.reserve("i1", "svc", 0.0, enforce=True, active_of=none_active)
    limits.commit(token)
    limits.reserve("i1", "svc", 1.0, enforce=True, active_of=none_active)
    assert limits.count("svc", 1.0) == 2


def test_eleventh_reservation_in_an_hour_is_rate_limited():
    limits = ProposalLimits()
    for n in range(MAX_PROPOSALS_PER_SERVICE_HOUR):
        limits.reserve(f"i{n}", "svc", float(n), enforce=True, active_of=none_active)
    with pytest.raises(ProposalRateLimited) as err:
        limits.reserve("i99", "svc", 10.0, enforce=True, active_of=none_active)
    assert 1 <= err.value.retry_after <= 3600
    limits.reserve("i99", "other", 10.0, enforce=True, active_of=none_active)
    limits.reserve("i99b", "svc", 3600.5, enforce=True, active_of=none_active)  # the first one has aged out


def test_unassigned_is_its_own_key_and_empty_service_maps_to_it():
    limits = ProposalLimits()
    for n in range(10):
        limits.reserve(f"i{n}", "", 0.0, enforce=True, active_of=none_active)
    with pytest.raises(ProposalRateLimited):
        limits.reserve("x", "unassigned", 1.0, enforce=True, active_of=none_active)
    limits.reserve("x", "svc", 1.0, enforce=True, active_of=none_active)


def test_release_frees_the_slot():
    limits = ProposalLimits()
    tokens = [limits.reserve(f"i{n}", "svc", 0.0, enforce=True, active_of=none_active) for n in range(10)]
    limits.release(tokens[3])
    limits.reserve("new", "svc", 1.0, enforce=True, active_of=none_active)


def test_seed_makes_a_restart_keep_the_window():
    limits = ProposalLimits()
    limits.seed({"svc": [4000.0 + n for n in range(10)], "old": [0.0] * 10}, now=5000.0)
    with pytest.raises(ProposalRateLimited):
        limits.reserve("i1", "svc", 5000.0, enforce=True, active_of=none_active)
    limits.reserve("i1", "old", 5000.0, enforce=True, active_of=none_active)  # seeded times older than 3600 s ignored
