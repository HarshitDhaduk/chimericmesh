"""``Hub.create_proposal`` through the approval seam, in both modes (task 5.2)."""
from __future__ import annotations

import asyncio
import os

import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_reduce import Gate, db_rule, fields

from chimericmesh.approvals.errors import ActiveProposalExists, ProposalInvalid, ProposalRateLimited
from chimericmesh.approvals.models import ApprovalPlan, RuleFields
from chimericmesh.hub import Hub
from chimericmesh.store import StoreError

MODES = ["bare", "store"]
WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa"]
GOOD = {"kind": "code_change", "files": ["upstream.py"], "estimated_lines": 10, "verification": "run tests"}


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def setup(rig, **rule_kw):
    await rig.add_service()
    await rig.manager.create_rule(RuleFields("api-gw", "signature", "disk full on /var", **rule_kw), actor="a")
    return await open_incident(rig.hub)


def bot_lines(rig) -> list[str]:
    return [m.text for m in rig.hub.transcript if m.kind == "bot" and m.text.startswith(("PROPOSAL", "AUTO-APPROVED"))]


def test_a_bare_hub_without_an_approver_is_unchanged():
    hub = Hub()

    async def go():
        iid = await open_incident(hub)
        prop = await hub.create_proposal(iid, "fix", "d")
        assert hub.approver is None and prop.status == "pending" and hub.enforce_limits is False
        for n in range(205):
            await hub.create_proposal(iid, f"fix {n}")
        assert len(hub.list_proposals()) == 200

    asyncio.run(go())


def test_a_matching_rule_approves_the_proposal_in_the_create_response(rig):
    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "free up space on /var", "details", extra=GOOD)
        assert prop.status == "approved" and prop.decided_by == "rule:r1" and prop.approved_by_rule == "r1"
        assert prop.decision_origin == "hub" and prop.files == ["upstream.py"] and prop.kind == "code_change"
        assert rig.hub.get_proposal(prop.id).status == "approved"
        rows = [r for r in await rig.audit(proposal_id=prop.id) if r["action"] == "proposal_auto_approved"]
        assert len(rows) == 1 and rows[0]["actor"] == "rule:r1" and rows[0]["service_id"] == "api-gw"
        assert rows[0]["rule_id"] == "r1" and "files=upstream.py" in rows[0]["detail"]
        assert f"incident={iid}" in rows[0]["detail"] and "remaining_hour=2" in rows[0]["detail"]
        lines = bot_lines(rig)
        assert len(lines) == 1 and lines[0].startswith(f"AUTO-APPROVED {prop.id} by rule r1 (service api-gw)")
        assert "reply !approve" not in lines[0]
        assert rig.manager.rule("r1").use_count in (0, 1)       # mirror count is not refreshed until a reload

    asyncio.run(go())


def test_a_proposal_that_does_not_match_stays_pending_and_says_why(rig):
    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "fix", extra={**GOOD, "estimated_lines": 500})
        assert prop.status == "pending" and prop.approved_by_rule is None and prop.decided_by == ""
        line = bot_lines(rig)[0]
        assert line.startswith(f"PROPOSAL {prop.id} for {iid}") and f"reply !approve {prop.id}" in line
        rows = [r for r in await rig.audit(proposal_id=prop.id) if r["action"] == "auto_approval_skipped"]
        assert [(r["rule_id"], r["detail"]) for r in rows] == [("r1", "condition=lines")]
        plain = await rig.hub.create_proposal(iid, "no structure at all")
        assert plain.status == "pending"

    asyncio.run(go())


def test_limit_errors_surface_only_when_enforced():
    rig = make_manager("store", enforce_limits=True)

    async def go():
        iid = await setup(rig)
        first = await rig.hub.create_proposal(iid, "one")
        with pytest.raises(ActiveProposalExists) as err:
            await rig.hub.create_proposal(iid, "two")
        assert err.value.proposal_id == first.id
        await rig.hub.decide(first.id, False, "alice")
        for n in range(9):
            await rig.hub.create_proposal(await open_incident(rig.hub, message=f"other failure kind {WORDS[n]}"), "x")
        with pytest.raises(ProposalRateLimited):
            await rig.hub.create_proposal(await open_incident(rig.hub, message="one more failure"), "x")
        rig.hub.enforce_limits = False
        await rig.hub.create_proposal(iid, "no limits now")

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_unknown_incident_and_forbidden_extra_keys(rig):
    async def go():
        await setup(rig)
        assert await rig.hub.create_proposal("nope", "x") is None
        iid = rig.hub.incidents()[0].id
        for key in ("status", "decided_by", "approved_by_rule"):
            with pytest.raises(ValueError):
                await rig.hub.create_proposal(iid, "x", extra={key: "approved"})
        with pytest.raises(ProposalInvalid):
            await rig.hub.create_proposal(iid, "x", extra={"files": ["../../etc/x.py"]})

    asyncio.run(go())


def test_the_reservation_is_released_when_the_commit_fails(rig, monkeypatch):
    async def go():
        iid = await setup(rig, max_per_hour=1)
        if rig.mode == "store":
            def boom(self, tx, prop):
                raise RuntimeError("write failed")
            monkeypatch.setattr(ApprovalPlan, "write_in_tx", boom)
            with pytest.raises(RuntimeError):
                await rig.hub.create_proposal(iid, "fix", extra=GOOD)
            monkeypatch.undo()
            assert rig.hub.list_proposals() == []
            assert await rig.store.read(lambda tx: tx.one("SELECT COUNT(*) FROM proposals")[0]) == 0
        else:
            real = rig.hub.say

            async def broken_say(*a):
                raise RuntimeError("irc down")

            rig.hub.say = broken_say
            try:
                await rig.hub.create_proposal(iid, "fix", extra=GOOD)    # a failing announcement never undoes it
            except RuntimeError:
                pass
            rig.hub.say = real
            rig.manager._ledger._uses.clear()
        # the quota slot is free again: the next proposal can be auto-approved
        again = await rig.hub.create_proposal(iid, "fix again", extra=GOOD)
        assert again.status == "approved"
        if rig.mode == "store":
            assert again.id == "p1"                                         # no id consumed by the rolled back write

    asyncio.run(go())


def test_five_concurrent_creates_against_an_hourly_limit_of_three(rig):
    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=3), actor="a")
        ids = [await open_incident(rig.hub, message=f"disk full on volume {WORDS[n]} please") for n in range(5)]
        real = rig.store.transact

        async def delayed(fn):
            await asyncio.sleep(0.05)
            return await real(fn)

        rig.store.transact = delayed
        out = await asyncio.gather(*(rig.hub.create_proposal(i, "fix", extra=GOOD) for i in ids))
        rig.store.transact = real
        assert sorted(p.status for p in out) == ["approved", "approved", "approved", "pending", "pending"]

    asyncio.run(go())


def test_a_cancelled_caller_cannot_split_commit_mirror_and_announcement(rig):
    async def go():
        iid = await setup(rig)
        gate = Gate(rig.store, hold=1) if rig.mode == "store" else None
        if gate is None:
            real = rig.hub.say
            release = asyncio.Event()

            async def slow_say(*a):
                await release.wait()
                return await real(*a)

            rig.hub.say = slow_say
        task = asyncio.ensure_future(rig.hub.create_proposal(iid, "fix", extra=GOOD))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0)
        if gate is not None:
            gate.release.set()
        else:
            release.set()
        await asyncio.sleep(0.3)
        if gate is not None:
            gate.restore()
        else:
            rig.hub.say = real
        props = rig.hub.list_proposals()
        assert len(props) == 1 and props[0].status == "approved"
        assert len([t for t in bot_lines(rig) if t.startswith("AUTO-APPROVED")]) == 1
        assert rig.manager._ledger.approved_at(props[0].id) is not None

    asyncio.run(go())


# ---- review M1: a revoke that runs while the create is in flight --------------------------------------------

def test_revoke_during_an_in_flight_create_denies_the_new_proposal():
    rig = make_manager("store")

    async def go():
        iid = await setup(rig)
        gate = Gate(rig.store, hold=1)
        creating = asyncio.ensure_future(rig.hub.create_proposal(iid, "fix", extra=GOOD))
        await asyncio.wait_for(gate.reached.wait(), 5)
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        gate.release.set()
        prop = await creating
        gate.restore()
        assert prop.status == "denied" and prop.decided_by == "rule-revoked:r1" and prop.decision_origin == "hub"
        lines = bot_lines(rig)
        assert any("denied" in t and "r1" in t and "revoked" in t for t in lines)
        assert not any(t.startswith("AUTO-APPROVED") for t in lines)
        uses = await rig.store.read(lambda tx: tx.one("SELECT COUNT(*) FROM rule_uses")[0])
        count = (await db_rule(rig, "r1")).use_count
        assert uses == 1 and count == 1                                    # the use was spent

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_revoke_after_the_proposal_is_mirrored_denies_it(rig):
    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "fix", extra=GOOD)
        assert prop.status == "approved"
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        assert rig.hub.get_proposal(prop.id).status == "denied"

    asyncio.run(go())


def test_bare_persist_writes_one_transaction_and_survives_a_failing_store():
    rig = make_manager("bare")

    async def go():
        iid = await setup(rig)
        prop = await rig.hub.create_proposal(iid, "fix", extra=GOOD)
        assert await rig.store.read(lambda tx: tx.one("SELECT COUNT(*) FROM rule_uses")[0]) == 1
        assert (await db_rule(rig, "r1")).use_count == 1
        assert "proposal_auto_approved" in await rig.actions(proposal_id=prop.id)
        iid2 = await open_incident(rig.hub, message="disk full on /var again and more")
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=3), actor="a")
        Gate(rig.store, fail=set(range(1, 50)))
        second = await rig.hub.create_proposal(iid2, "fix", extra=GOOD)          # persist fails: logged, not raised
        assert second.status == "approved"
        assert rig.manager._ledger.approved_at(second.id) is not None

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_shares_store_in_both_modes():
    bare, full = make_manager("bare"), make_manager("store")
    try:
        assert bare.manager.shares_store(bare.hub.store) is False
        assert full.manager.shares_store(full.hub.store) is True
    finally:
        bare.close()
        full.close()
