"""Completion checks (task 5.3, R6.4 to R6.6) and the cooldown flow through complete."""
from __future__ import annotations

import asyncio

import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_create import GOOD, WORDS, bot_lines
from test_approvals_reduce import Gate, db_rule

from chimericmesh.approvals.errors import ProposalExpired, ProposalInvalid
from chimericmesh.approvals.models import RuleFields

MODES = ["bare", "store"]
H = 3600.0


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def setup(rig, n: int = 1, **rule_kw):
    await rig.add_service()
    await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", **rule_kw), actor="a")
    ids = [await open_incident(rig.hub, message=f"disk full on volume {WORDS[i]} please") for i in range(n)]
    return ids


async def auto_proposal(rig, iid):
    prop = await rig.hub.create_proposal(iid, "free space", extra=GOOD)
    assert prop.status == "approved" and prop.approved_by_rule == "r1"
    return prop


OK = {"files_changed": ["upstream.py"], "lines_changed": 20, "verification_result": "passed"}


def test_a_clean_completion_is_recorded_and_leaves_the_rule_active(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        done = await rig.hub.complete(prop.id, "fixed", extra=OK)
        assert done.status == "done" and done.files_changed == ["upstream.py"] and done.lines_changed == 20
        assert done.verification_result == "passed" and done.result_note == "fixed"
        assert rig.manager.rule("r1").status == "active"
        assert "completion_checked" in await rig.actions(proposal_id=prop.id)
        assert rig.manager._ledger.completed_uses() == [(iid, 1, prop.id)]
        if rig.mode == "store":
            assert await rig.store.read(lambda tx: tx.one("SELECT completed_at FROM rule_uses")[0]) == rig.clock.t

    asyncio.run(go())


@pytest.mark.parametrize("bad,reason", [
    ({"files_changed": ["../elsewhere.py"]}, "completion_out_of_scope"),
    ({"files_changed": [".git/hooks/pre-commit.py"]}, "completion_out_of_scope"),
    ({"files_changed": ["settings.json"]}, "completion_out_of_scope"),
    ({"files_changed": [f"f{n}.py" for n in range(6)]}, "files_over_limit"),
    ({"files_changed": ["a.py"], "lines_changed": 101}, "lines_over_limit"),
    ({"files_changed": ["a.py"], "verification_result": "failed"}, "verification_failed"),
])
def test_a_violation_completes_the_proposal_but_suspends_the_rule(rig, bad, reason):
    async def go():
        a, b = await setup(rig, 2)
        first = await auto_proposal(rig, a)
        other = await auto_proposal(rig, b)
        said = len(rig.announced)
        done = await rig.hub.complete(first.id, "done", extra=bad)
        assert done.status == "done"
        rule = rig.manager.rule("r1")
        assert rule.status == "suspended" and rule.status_reason == reason
        assert (await db_rule(rig, "r1")).status == "suspended"
        suspended = [r for r in await rig.audit(rule_id="r1") if r["action"] == "rule_suspended"]
        assert len(suspended) == 1 and suspended[0]["detail"] == reason and suspended[0]["actor"] == "hub"
        denied = rig.hub.get_proposal(other.id)
        assert denied.status == "denied" and denied.decided_by == "rule-suspended:r1"
        assert len([t for t in rig.announced[said:] if "suspended" in t]) == 1

    asyncio.run(go())


def test_a_completion_without_details_is_unverified_and_leaves_the_rule_active(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        done = await rig.hub.complete(prop.id, "done")
        assert done.status == "done" and rig.manager.rule("r1").status == "active"
        actions = await rig.actions(proposal_id=prop.id)
        assert "completion_unverified" in actions and "completion_checked" not in actions

    asyncio.run(go())


def test_a_user_approved_proposal_is_not_checked(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "manual", extra={"estimated_lines": 999})
        await rig.hub.decide(prop.id, True, "alice")
        done = await rig.hub.complete(prop.id, "x", extra={"files_changed": ["../x.py"], "verification_result": "failed"})
        assert done.status == "done" and rig.manager.rule("r1").status == "active"
        assert not [a for a in await rig.actions(proposal_id=prop.id) if a.startswith("completion")]

    asyncio.run(go())


def test_the_note_is_cut_to_300_characters(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        done = await rig.hub.complete(prop.id, "n" * 900, extra=OK)
        assert len(done.result_note) == 300

    asyncio.run(go())


def test_invalid_completion_fields_are_rejected_before_anything_changes(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        for extra in ({"files_changed": ["a"] * 51}, {"lines_changed": -1}, {"verification_result": "maybe"},
                      {"lines_changed": True}):
            with pytest.raises(ProposalInvalid):
                await rig.hub.complete(prop.id, "x", extra=extra)
        assert rig.hub.get_proposal(prop.id).status == "approved"

    asyncio.run(go())


def test_complete_after_the_sweep_denied_the_approval_returns_none(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        rig.clock.advance(1801)
        await rig.manager.sweep_once()
        assert await rig.hub.complete(prop.id, "late", extra=OK) is None

    asyncio.run(go())


def test_complete_makes_an_expired_approval_visible_without_waiting_for_the_sweep(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        rig.clock.advance(1801)
        assert await rig.hub.complete(prop.id, "late", extra=OK) is None
        assert rig.hub.get_proposal(prop.id).status == "denied"

    asyncio.run(go())


def test_with_a_failing_store_the_1800_second_rule_still_holds(rig):
    async def go():
        (iid,) = await setup(rig)
        prop = await auto_proposal(rig, iid)
        gate = Gate(rig.store, fail=set(range(1, 100))) if rig.mode == "store" else None
        rig.clock.advance(1801)
        with pytest.raises(ProposalExpired):
            rig.manager.validate_completion(prop.id, {})
        if gate is not None:
            assert await rig.hub.complete(prop.id, "late") is None
            gate.restore()
        (iid2,) = await setup_second(rig)
        prop2 = await auto_proposal(rig, iid2)
        rig.clock.advance(1800)
        assert (await rig.hub.complete(prop2.id, "just in time", extra=OK)).status == "done"

    async def setup_second(rig):
        return [await open_incident(rig.hub, message="disk full on volume omega please")]

    asyncio.run(go())


# ---- the cooldown flow through a completed fix (R4.9) ------------------------------------------------------

def test_a_reopen_one_hour_after_an_auto_fix_needs_the_user_and_one_line_says_why():
    rig = make_manager("store")
    rig.hub.lifecycle.reopen_after = 300

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_incident=3), actor="a")
        iid = await open_incident(rig.hub, message="disk full on volume alpha please")
        first = await auto_proposal(rig, iid)
        await rig.hub.complete(first.id, "ok", extra=OK)
        await rig.hub.resolve(iid, "fixed")
        rig.clock.advance(H)
        for _ in range(3):
            await open_incident(rig.hub, message="disk full on volume alpha please", count=1)
        assert rig.hub.get_incident(iid).opening == 2
        before = len(bot_lines(rig))
        second = await rig.hub.create_proposal(iid, "fix again", extra=GOOD)
        assert second.status == "pending" and second.approved_by_rule is None
        new = bot_lines(rig)[before:]
        assert len(new) == 1 and new[0].startswith(f"PROPOSAL {second.id}") and first.id in new[0]
        rows = [r for r in await rig.audit(proposal_id=second.id) if r["action"] == "autofix_cooldown"]
        assert len(rows) == 1 and f"earlier={first.id}" in rows[0]["detail"] and rows[0]["rule_id"] == "r1"
        # 30 hours later, the same opening is still waiting for the user (H1)
        rig.clock.advance(29 * H)
        await rig.hub.decide(second.id, False, "alice")
        third = await rig.hub.create_proposal(iid, "fix a third time", extra=GOOD)
        assert third.status == "pending"

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_a_reopen_after_the_cooldown_is_auto_approved_again():
    rig = make_manager("store")
    rig.hub.lifecycle.reopen_after = 300

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_incident=3), actor="a")
        iid = await open_incident(rig.hub, message="disk full on volume alpha please")
        first = await auto_proposal(rig, iid)
        await rig.hub.complete(first.id, "ok", extra=OK)
        await rig.hub.resolve(iid, "fixed")
        rig.clock.advance(25 * H)
        for _ in range(3):
            await open_incident(rig.hub, message="disk full on volume alpha please", count=1)
        second = await rig.hub.create_proposal(iid, "fix again", extra=GOOD)
        assert second.status == "approved" and second.approved_by_rule == "r1"

    try:
        asyncio.run(go())
    finally:
        rig.close()
