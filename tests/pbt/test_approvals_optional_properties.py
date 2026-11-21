"""Optional properties 14, 19 and 23 of auto-approval (task 9.2)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_helpers import make_manager, open_incident  # noqa: E402

from chimericmesh.approvals.errors import ActiveProposalExists, ProposalRateLimited  # noqa: E402
from chimericmesh.approvals.models import RuleFields  # noqa: E402

SETTINGS = settings(max_examples=15, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow])
GOOD = {"kind": "code_change", "files": ["upstream.py"], "estimated_lines": 10, "verification": "run tests"}
WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu",
         "nu", "xi"]


# ---- Property 14: pause blocks all auto-approval ---------------------------------------------------------------------
@SETTINGS
@given(n_rules=st.integers(1, 4), n_props=st.integers(1, 5))
def test_property_14_nothing_is_auto_approved_while_paused(n_rules, n_props):
    rig = make_manager("bare")

    async def go():
        await rig.add_service()
        for _ in range(n_rules):
            await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=10), actor="a")
        await rig.manager.pause(actor="a", key="k")
        for i in range(n_props):
            iid = await open_incident(rig.hub, message=f"disk full on volume {WORDS[i]} please")
            prop = await rig.hub.create_proposal(iid, "fix", extra=GOOD)
            assert prop.status == "pending" and prop.approved_by_rule is None
        assert all(r.status == "active" for r in rig.manager.rules())       # pause changes no rule

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 19: proposal creation limits ------------------------------------------------------------------------------
@SETTINGS
@given(enforce=st.booleans(), same_incident=st.integers(1, 3), others=st.integers(0, 12))
def test_property_19_limits_hold_only_when_enforced(enforce, same_incident, others):
    rig = make_manager("store", enforce_limits=enforce)

    async def go():
        await rig.add_service()
        iid = await open_incident(rig.hub, message="disk full on volume omega please")
        made, refused_active, refused_rate = 0, 0, 0
        for _ in range(same_incident):
            try:
                await rig.hub.create_proposal(iid, "fix")
                made += 1
            except ActiveProposalExists:
                refused_active += 1
        for i in range(others):
            other = await open_incident(rig.hub, message=f"disk full on volume {WORDS[i]} please")
            try:
                await rig.hub.create_proposal(other, "fix")
                made += 1
            except ProposalRateLimited:
                refused_rate += 1
        if not enforce:
            assert made == same_incident + others and refused_active == refused_rate == 0
        else:
            assert refused_active == same_incident - 1
            assert made == min(1 + others, 10)
            assert len(rig.hub.list_proposals()) <= 10

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 23: approval derives from the column ------------------------------------------------------------------------
@SETTINGS
@given(decider=st.sampled_from(["alice", "rule:r1", "RULE:r9", "hub", "rule-revoked:r1"]), by_rule=st.booleans())
def test_property_23_approval_by_follows_approved_by_rule_only(decider, by_rule):
    rig = make_manager("bare")

    async def go():
        await rig.add_service()
        iid = await open_incident(rig.hub)
        prop = await rig.hub.create_proposal(iid, "fix")
        if by_rule:
            prop = await rig.hub.hub_transition(prop.id, "approved", decided_by="rule:r1", approved_by_rule="r1")
        else:
            prop.status, prop.decided_by, prop.decided_at = "approved", decider, rig.clock.t   # text says anything
        view = rig.manager.approval_of(prop)
        assert view["by"] == ("rule" if by_rule else "user")
        assert view["rule_id"] == ("r1" if by_rule else None)

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_optional_properties_are_collected():
    pytest.importorskip("hypothesis")
