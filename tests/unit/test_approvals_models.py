from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import logging

from chimericmesh import diagnostics
from chimericmesh.approvals.limits import MAX_PROPOSALS_PER_SERVICE_HOUR
from chimericmesh.approvals.models import BOUNDS, CATEGORIES, ApprovalSettings


def test_categories_equal_what_diagnostics_can_emit():
    emitted = {rule[0] for rule in diagnostics.RULES} | {diagnostics._GENERIC[0]}
    assert set(CATEGORIES) == emitted


def test_hourly_cap_constant():
    assert MAX_PROPOSALS_PER_SERVICE_HOUR == 10
    assert BOUNDS["max_per_hour"]["max"] == 10


def test_settings_defaults():
    s = ApprovalSettings.from_env({})
    assert (s.autofix_cooldown, s.proposal_ttl, s.rule_approval_ttl, s.sweep_interval) == (86400, 24 * 3600, 1800, 60)


def test_settings_accept_in_range_values():
    s = ApprovalSettings.from_env({"CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS": "30",
                                   "CHIMERICMESH_PROPOSAL_TTL_HOURS": "2"})
    assert s.autofix_cooldown == 30 and s.proposal_ttl == 7200


def test_out_of_range_values_warn_and_use_defaults(caplog):
    env = {"CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS": "3000000", "CHIMERICMESH_PROPOSAL_TTL_HOURS": "0"}
    with caplog.at_level(logging.WARNING, logger="chimericmesh.approvals"):
        s = ApprovalSettings.from_env(env)
    assert s.autofix_cooldown == 86400 and s.proposal_ttl == 24 * 3600
    assert len(caplog.records) == 2


def test_garbage_and_nan_use_defaults():
    for bad in ("abc", "nan", "inf", "-1"):
        s = ApprovalSettings.from_env({"CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS": bad})
        assert s.autofix_cooldown == 86400
