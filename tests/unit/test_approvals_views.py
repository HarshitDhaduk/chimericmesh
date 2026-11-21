"""Approval views and status (task 4.6)."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import dataclasses

import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_reduce import fields, rule_approved

from chimericmesh.approvals.models import BOUNDS

MODES = ["bare", "store"]


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def setup(rig, n=1):
    await rig.add_service()
    for i in range(n):
        await rig.manager.create_rule(fields(match_value=f"sig{i}"), actor="a")
    return await open_incident(rig.hub)


def test_nobody_approved_gives_none(rig):
    async def go():
        iid = await setup(rig)
        pending = await rig.hub.create_proposal(iid, "a")
        assert rig.manager.approval_of(pending) is None
        await rig.hub.decide(pending.id, False, "alice")
        assert rig.manager.approval_of(rig.hub.get_proposal(pending.id)) is None

    asyncio.run(go())


def test_user_approval(rig):
    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "a")
        await rig.hub.decide(prop.id, True, "rule:r1")      # a user typing a rule-looking name via the store seam
        got = rig.hub.get_proposal(prop.id)
        view = rig.manager.approval_of(got)
        assert view == {"by": "user", "rule_id": None, "rule_status_now": None, "approved_at": got.decided_at,
                        "expired": False}

    asyncio.run(go())


def test_rule_approval_follows_the_column_and_the_rule_state(rig):
    async def go():
        iid = await setup(rig)
        prop = await rule_approved(rig, iid, "r1")
        view = rig.manager.approval_of(prop)
        assert view["by"] == "rule" and view["rule_id"] == "r1" and view["rule_status_now"] == "active"
        assert view["expired"] is False
        active = rig.manager.rule("r1")
        rig.manager._book.replace(dataclasses.replace(active, status="suspended"))   # before any denial reaches it
        assert rig.manager.approval_of(prop)["rule_status_now"] == "suspended"
        assert rig.manager.approval_of(prop)["expired"] is True
        rig.manager._book.replace(active)
        assert rig.manager.approval_of(prop)["expired"] is False
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        denied = rig.hub.get_proposal(prop.id)                  # the revoke denied it, but the column stays
        assert rig.manager.approval_of(denied)["rule_id"] == "r1"

    asyncio.run(go())


def test_expired_flag_at_the_1800_second_boundary_and_by_clock_before_the_sweep(rig):
    async def go():
        iid = await setup(rig)
        prop = await rule_approved(rig, iid, "r1")
        rig.clock.advance(1800)
        assert rig.manager.approval_of(prop)["expired"] is False
        rig.clock.advance(1)
        assert rig.manager.approval_of(prop)["expired"] is True
        rig.clock.advance(7 * 86400)
        assert rig.manager.approval_of(prop)["rule_status_now"] == "expired"   # computed from expires_at, no sweep

    asyncio.run(go())


def test_pruned_use_row_gives_no_time_and_does_not_raise(rig):
    async def go():
        iid = await setup(rig)
        prop = await rule_approved(rig, iid, "r1")
        assert rig.manager.approval_of(prop)["approved_at"] is None            # no rule_uses row: never a guess
        rig.manager._ledger.seed([], [])

    asyncio.run(go())


def test_proposal_view_adds_the_approval_key(rig):
    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "a")
        view = rig.manager.proposal_view(prop)
        assert view == {**prop.to_full_dict(), "approval": None}

    asyncio.run(go())


def test_status_reports_flags_counts_and_bounds(rig):
    async def go():
        await setup(rig, 3)
        await rig.manager.pause_rule("r2", actor="a", key="k")
        await rig.manager.pause(actor="irc:bob", key="bob")
        st = rig.manager.status().to_dict()
        assert st["paused"] is True and st["paused_by"] == "irc:bob" and st["paused_at"] == rig.clock.t
        assert (st["active_rules"], st["suspended_rules"]) == (2, 1)
        assert st["cooldown_seconds"] == 86400 and st["proposal_ttl_hours"] == 24
        b = st["bounds"]
        assert b == {k: dict(v) for k, v in BOUNDS.items()}
        assert (b["max_per_hour"]["min"], b["max_per_hour"]["max"]) == (1, 10)
        assert (b["max_per_incident"]["min"], b["max_per_incident"]["max"]) == (1, 3)
        assert (b["max_files"]["max"], b["max_lines"]["max"]) == (20, 500)
        assert b["expires_days"]["default"] == 7 and b["expires_days"]["max"] == 30

    asyncio.run(go())


def test_rules_are_ordered_by_number_and_unknown_is_none(rig):
    async def go():
        await setup(rig, 11)
        assert [r.id for r in rig.manager.rules()] == [f"r{n}" for n in range(1, 12)]
        assert rig.manager.rule("rX") is None
        d = rig.manager.rule_dict(rig.manager.rule("r1"))
        assert d["uses_last_hour"] == 0 and d["remaining_this_hour"] == 3 and "code_folder" in d

    asyncio.run(go())
