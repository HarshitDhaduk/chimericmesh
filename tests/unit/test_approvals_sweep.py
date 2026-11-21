"""Sweep, expiry on read and withdraw (task 4.4). A fake clock drives everything; no sweep task runs."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import logging

import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_reduce import Gate, db_rule, fields, rule_approved

from chimericmesh.approvals.ledger import UsageRow
from chimericmesh.approvals.manager import SWEEP_STEPS
from chimericmesh.approvals.rulebook import insert_use
from chimericmesh.store import NotFound, StoreError

MODES = ["bare", "store"]
HOUR = 3600.0


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def with_rule(rig, n: int = 1):
    await rig.add_service()
    for i in range(n):
        await rig.manager.create_rule(fields(match_value=f"sig{i}"), actor="a")


def test_an_expired_rule_is_marked_audited_announced_and_its_proposals_denied(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        said = len(rig.announced)
        rig.clock.advance(8 * 86400)
        await rig.manager.sweep_once()
        assert rig.manager.rule("r1").status == "expired" and (await db_rule(rig, "r1")).status == "expired"
        assert (await rig.actions(rule_id="r1")).count("rule_expired") == 1
        assert [t for t in rig.announced[said:] if "expired" in t and "r1" in t]
        p = rig.hub.get_proposal(prop.id)
        assert p.status == "denied" and p.decided_by == "rule-expired:r1" and p.decision_origin == "hub"
        await rig.manager.sweep_once()
        assert (await rig.actions(rule_id="r1")).count("rule_expired") == 1      # once only

    asyncio.run(go())


def test_a_rule_approval_older_than_1800_seconds_is_denied_and_cannot_complete(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        user = await rig.hub.create_proposal(iid, "manual")
        await rig.hub.decide(user.id, True, "alice")
        rig.clock.advance(1800)
        await rig.manager.sweep_once()
        assert rig.hub.get_proposal(prop.id).status == "approved"               # exactly 1800: still valid
        rig.clock.advance(1)
        await rig.manager.sweep_once()
        p = rig.hub.get_proposal(prop.id)
        assert p.status == "denied" and p.decided_by == "rule-expired:r1"
        assert await rig.hub.complete(prop.id, "late") is None
        rig.clock.advance(30 * 86400)
        await rig.manager.sweep_once()
        assert rig.hub.get_proposal(user.id).status == "approved"                # a manual approval never expires

    asyncio.run(go())


def test_old_pending_proposals_expire_with_the_hub_origin(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rig.hub.create_proposal(iid, "fix")
        rig.clock.advance(24 * HOUR - 1)
        await rig.manager.sweep_once()
        assert rig.hub.get_proposal(prop.id).status == "pending"
        rig.clock.advance(2)
        await rig.manager.sweep_once()
        p = rig.hub.get_proposal(prop.id)
        assert p.status == "denied" and p.decided_by == "expired:ttl" and p.decision_origin == "hub"

    asyncio.run(go())


def test_expire_due_does_the_same_work_cheaply(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        gate = Gate(rig.store)
        await rig.manager.expire_due()
        assert gate.calls == 0                                                  # nothing due: no store work
        rig.clock.advance(1801)
        await rig.manager.expire_due()
        gate.restore()
        assert rig.hub.get_proposal(prop.id).status == "denied"

    asyncio.run(go())


def test_repairs(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        stuck = await rule_approved(rig, iid, "r1")
        import dataclasses
        revoked = dataclasses.replace(rig.manager.rule("r1"), status="revoked", status_reason="test")
        rig.manager._book.replace(revoked)                                      # mirror revoked, nothing persisted
        rig.manager._book.mark_dirty("r1")
        await rig.manager.sweep_once()
        assert rig.hub.get_proposal(stuck.id).decided_by == "rule-revoked:r1"
        assert (await db_rule(rig, "r1")).status == "revoked"                  # the dirty reduce was persisted too

    asyncio.run(go())


def test_done_proposal_without_completed_at_gets_one(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        await rig.hub.complete(prop.id, "ok")
        now = rig.clock.t
        await rig.store.transact(lambda tx: insert_use(tx, rule_id="r1", proposal_id=prop.id, incident_id=iid,
                                                       opening=1, service_id="api-gw", approved_at=now - 5))
        rig.manager._ledger.seed([UsageRow("r1", prop.id, iid, 1, "api-gw", now - 5, None)], [])
        await rig.manager.sweep_once()
        rows = await rig.store.read(lambda tx: [tuple(r) for r in tx.all(
            "SELECT proposal_id, completed_at FROM rule_uses")])
        assert rows == [(prop.id, now)]
        assert rig.manager._ledger.completed_uses() == [(iid, 1, prop.id)]

    asyncio.run(go())


def test_observed_reopens_are_recorded_once_and_the_first_time_wins(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        now = rig.clock.t
        rig.manager._ledger.seed([UsageRow("r1", "p1", iid, 1, "api-gw", now - 5, now - 4)], [])
        await rig.manager.sweep_once()
        assert rig.manager._ledger.reopen_at(iid, 1) is None                   # no reopen yet
        rig.hub.get_incident(iid).opening = 2
        await rig.manager.sweep_once()
        first = rig.manager._ledger.reopen_at(iid, 2)
        assert first == now
        rig.clock.advance(500)
        await rig.manager.sweep_once()
        assert rig.manager._ledger.reopen_at(iid, 2) == first
        rows = await rig.store.read(lambda tx: [tuple(r) for r in tx.all("SELECT incident_id, opening, first_seen_at "
                                                                         "FROM incident_openings")])
        assert rows == [(iid, 2, first)]

    asyncio.run(go())


def test_rows_older_than_31_days_are_pruned(rig):
    async def go():
        await with_rule(rig)
        now = rig.clock.t
        await rig.store.transact(lambda tx: insert_use(tx, rule_id="r1", proposal_id="p1", incident_id="i1",
                                                       opening=1, service_id="api-gw", approved_at=now - 32 * 86400))
        await rig.store.transact(lambda tx: insert_use(tx, rule_id="r1", proposal_id="p2", incident_id="i1",
                                                       opening=1, service_id="api-gw", approved_at=now - 86400))
        await rig.manager.sweep_once()
        rows = await rig.store.read(lambda tx: [r[0] for r in tx.all("SELECT proposal_id FROM rule_uses")])
        assert rows == ["p2"]

    asyncio.run(go())


def test_a_failing_step_does_not_stop_the_others_and_the_order_is_fixed(rig):
    async def go():
        await with_rule(rig)
        order: list[str] = []
        for name in SWEEP_STEPS:
            original = getattr(rig.manager, name)

            async def wrapped(name=name, original=original):
                order.append(name)
                if name == "_step_expire":
                    raise RuntimeError("boom")
                await original()

            setattr(rig.manager, name, wrapped)
        await rig.manager.sweep_once()
        assert order == ["_step_persist_dirty", "_step_expire", "_step_deny_stale", "_step_ttl", "_step_repair",
                         "_step_reload", "_step_openings", "_step_prune"]
        assert tuple(order) == SWEEP_STEPS

    asyncio.run(go())


def test_the_sweep_persists_the_dirty_set_before_it_reloads(rig):
    async def go():
        await with_rule(rig)
        gate = Gate(rig.store, fail={1})
        await rig.manager.revoke_rule("r1", actor="a", key="k")                # persist fails: dirty
        gate.restore()
        assert (await db_rule(rig, "r1")).status == "active"
        await rig.manager.sweep_once()
        assert rig.manager.rule("r1").status == "revoked" and (await db_rule(rig, "r1")).status == "revoked"

    asyncio.run(go())


# ---- withdraw ----------------------------------------------------------------------------------------------

def test_withdraw_denies_a_pending_proposal_with_the_hub_origin(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rig.hub.create_proposal(iid, "fix")
        out = await rig.manager.withdraw(prop.id, actor="irc:" + "n" * 80, key="k")
        assert out.status == "denied" and out.decided_by.startswith("withdrawn:irc:") and len(out.decided_by) <= 60
        assert out.decision_origin == "hub"
        assert rig.limiter.calls[-1]["action"] == "proposal_withdrawn" and rig.limiter.calls[-1]["key"] == "k"

    asyncio.run(go())


def test_withdraw_frees_the_incident_when_limits_are_on():
    rig = make_manager("store", enforce_limits=True)

    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        manager_active = rig.manager._active_of(iid)
        assert manager_active is None
        prop = await rig.hub.create_proposal(iid, "fix")
        assert rig.manager._active_of(iid) == prop.id
        await rig.manager.withdraw(prop.id, actor="bob", key="k")
        assert rig.manager._active_of(iid) is None

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_withdraw_of_a_non_pending_proposal_changes_nothing(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rig.hub.create_proposal(iid, "fix")
        await rig.hub.decide(prop.id, True, "alice")
        await rig.hub.complete(prop.id, "ok")
        calls = len(rig.limiter.calls)
        out = await rig.manager.withdraw(prop.id, actor="bob", key="k")
        assert out.status == "done" and len(rig.limiter.calls) == calls
        with pytest.raises(NotFound):
            await rig.manager.withdraw("p999", actor="bob", key="k")

    asyncio.run(go())


# ---- review M5: expire_due is bounded and never raises -------------------------------------------------

def test_expire_due_returns_at_once_while_the_sweep_holds_the_lock(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        rig.clock.advance(1801)
        async with rig.manager._sweep_lock:
            await asyncio.wait_for(rig.manager.expire_due(), 0.5)
        assert rig.hub.get_proposal(prop.id).status == "approved"               # not done by expire_due

    asyncio.run(go())


def test_expire_due_gives_up_after_its_timeout_and_the_work_is_not_cancelled(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        rig.manager._expire_timeout = 0.1
        rig.clock.advance(1801)
        gate = Gate(rig.store, hold=1) if rig.mode == "store" else None
        if gate is None:
            real = rig.hub.hub_transition
            release = asyncio.Event()

            async def slow(*a, **k):
                await release.wait()
                return await real(*a, **k)

            rig.hub.hub_transition = slow
        started = asyncio.get_event_loop().time()
        await rig.manager.expire_due()                                          # returns, does not raise
        assert asyncio.get_event_loop().time() - started < 2
        if gate is not None:
            gate.release.set()
            await asyncio.sleep(0.3)
            gate.restore()
        else:
            release.set()
            await asyncio.sleep(0.1)
            rig.hub.hub_transition = real
        assert rig.hub.get_proposal(prop.id).status == "denied"                 # the work finished by itself

    asyncio.run(go())


def test_expire_due_swallows_store_errors_and_logs_once_a_minute(rig, caplog):
    async def go():
        await with_rule(rig)
        rig.clock.advance(8 * 86400)
        Gate(rig.store, fail={1, 2, 3, 4})
        with caplog.at_level(logging.WARNING, logger="chimericmesh.approvals"):
            await rig.manager.expire_due()
            await rig.manager.expire_due()
        assert len([r for r in caplog.records if "approvals" in r.name]) <= 2
        assert rig.manager.rules() is not None                                  # reads never raise

    asyncio.run(go())


def test_a_dead_store_still_shows_the_expired_flag_and_blocks_completion(rig):
    async def go():
        await with_rule(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        Gate(rig.store, fail=set(range(1, 100)))
        rig.clock.advance(1800)
        assert rig.manager.approval_of(rig.hub.get_proposal(prop.id))["expired"] is False
        rig.clock.advance(1)
        assert rig.manager.approval_of(rig.hub.get_proposal(prop.id))["expired"] is True
        from chimericmesh.approvals.errors import ProposalExpired
        with pytest.raises(ProposalExpired):
            rig.manager.validate_completion(prop.id, {})

    asyncio.run(go())
