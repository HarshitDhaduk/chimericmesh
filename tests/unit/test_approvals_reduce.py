"""Reduce and re-enable operations (task 4.2), including the ordering interleavings of review H2."""
from __future__ import annotations

import asyncio

import pytest
from approvals_helpers import FakeLimiter, make_manager, open_incident

from chimericmesh.approvals.errors import RuleConflict, RuleNotFound
from chimericmesh.approvals.manager import ApprovalManager
from chimericmesh.approvals.models import RuleFields, RulePatch
from chimericmesh.approvals.rulebook import load_rules, load_state
from chimericmesh.services.limiter import ReduceAuditLimiter
from chimericmesh.services.models import CLEAR, ServicePatch
from chimericmesh.store import StoreError

MODES = ["bare", "store"]


def fields(**kw) -> RuleFields:
    base = dict(service_id="api-gw", match_type="signature", match_value="disk full on /var")
    base.update(kw)
    return RuleFields(**base)


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def setup(rig, n: int = 1):
    await rig.add_service()
    return [await rig.manager.create_rule(fields(match_value=f"sig{i}"), actor="a") for i in range(n)]


async def rule_approved(rig, iid: str, rule_id: str):
    """A proposal that the hub approved by a rule (what ``prepare`` will produce in task 5)."""
    prop = await rig.hub.create_proposal(iid, "fix")
    return await rig.hub.hub_transition(prop.id, "approved", decided_by=f"rule:{rule_id}", approved_by_rule=rule_id)


async def db_rule(rig, rid: str):
    return next(r for r in await rig.store.read(load_rules) if r.id == rid)


class Gate:
    """Wrap ``store.transact``: call number ``n`` (1-based) can be held before or after its real write."""

    def __init__(self, store, *, hold: int | None = None, after: bool = False, fail: set[int] | None = None):
        self.store, self.real = store, store.transact
        self.hold, self.after, self.fail = hold, after, fail or set()
        self.calls = 0
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        store.transact = self

    async def __call__(self, fn):
        self.calls += 1
        n = self.calls
        if n in self.fail:
            raise StoreError("down")
        if n == self.hold and not self.after:
            self.reached.set()
            await self.release.wait()
        result = await self.real(fn)
        if n == self.hold and self.after:
            self.reached.set()
            await self.release.wait()
        return result

    def restore(self):
        self.store.transact = self.real


def test_revoke_marks_the_mirror_before_any_await(rig):
    async def go():
        await setup(rig)
        gate = Gate(rig.store, hold=1)
        task = asyncio.ensure_future(rig.manager.revoke_rule("r1", actor="a", key="k"))
        await asyncio.sleep(0.05)
        assert rig.manager.rule("r1").status == "revoked"        # the persist is still blocked
        gate.release.set()
        await task
        gate.restore()
        assert (await db_rule(rig, "r1")).status == "revoked"

    asyncio.run(go())


def test_revoke_denies_approved_proposals_of_the_rule_only(rig):
    async def go():
        await setup(rig, 2)
        iid = await open_incident(rig.hub)
        mine = await rule_approved(rig, iid, "r1")
        other = await rule_approved(rig, iid, "r2")
        user = await rig.hub.create_proposal(iid, "user fix")
        await rig.hub.decide(user.id, True, "alice")
        done = await rule_approved(rig, iid, "r1")
        await rig.hub.complete(done.id, "ok")
        await rig.manager.revoke_rule("r1", actor="bob", key="k")
        get = rig.hub.get_proposal
        assert get(mine.id).status == "denied" and get(mine.id).decided_by == "rule-revoked:r1"
        assert get(mine.id).decision_origin == "hub"
        assert get(other.id).status == "approved" and get(user.id).status == "approved"
        assert get(done.id).status == "done"

    asyncio.run(go())


def test_revoke_is_idempotent_and_needs_no_token(rig):
    async def go():
        await setup(rig)
        first = await rig.manager.revoke_rule("r1", actor="a", key="k")
        rows, said = len(rig.limiter.calls), len(rig.announced)
        again = await rig.manager.revoke_rule("r1", actor="a", key="k")
        assert again == first and len(rig.limiter.calls) == rows and len(rig.announced) == said
        import inspect
        assert "token" not in inspect.signature(ApprovalManager.revoke_rule).parameters

    asyncio.run(go())


def test_reduce_operations_are_never_refused_for_final_or_matching_states(rig):
    async def go():
        await setup(rig, 3)
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        await rig.manager.pause_rule("r2", actor="a", key="k")
        rig.clock.advance(8 * 86400)
        await rig.manager.sweep_once()                           # r3 expires; r2 (suspended) expires too
        assert rig.manager.rule("r3").status == "expired"
        calls, said = len(rig.limiter.calls), len(rig.announced)
        assert (await rig.manager.revoke_rule("r3", actor="a", key="k")).status == "expired"
        assert (await rig.manager.pause_rule("r3", actor="a", key="k")).status == "expired"
        assert (await rig.manager.pause_rule("r1", actor="a", key="k")).status == "revoked"
        assert (await rig.manager.pause_rule("r2", actor="a", key="k")).status == "expired"
        assert len(rig.limiter.calls) == calls and len(rig.announced) == said
        for call in (rig.manager.revoke_rule, rig.manager.pause_rule):
            with pytest.raises(RuleNotFound):
                await call("r99", actor="a", key="k")

    asyncio.run(go())


def test_pause_rule_and_enable_rule(rig):
    async def go():
        await setup(rig)
        paused = await rig.manager.pause_rule("r1", actor="a", key="k")
        assert paused.status == "suspended" and (await db_rule(rig, "r1")).status == "suspended"
        enabled = await rig.manager.enable_rule("r1", actor="admin")
        assert enabled.status == "active" and (await db_rule(rig, "r1")).status == "active"
        assert "rule_enabled" in await rig.actions(rule_id="r1")

    asyncio.run(go())


def test_enable_refusals(rig):
    async def go():
        await setup(rig, 3)
        with pytest.raises(RuleNotFound):
            await rig.manager.enable_rule("r9", actor="a")
        with pytest.raises(RuleConflict):                       # active
            await rig.manager.enable_rule("r1", actor="a")
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        with pytest.raises(RuleConflict):                       # revoked
            await rig.manager.enable_rule("r1", actor="a")
        await rig.manager.pause_rule("r2", actor="a", key="k")
        rig.clock.advance(8 * 86400)
        with pytest.raises(RuleConflict):                       # expired: becomes expired
            await rig.manager.enable_rule("r2", actor="a")
        assert rig.manager.rule("r2").status == "expired"

    asyncio.run(go())


def test_enable_refuses_when_the_folder_changed_or_the_service_is_gone(rig):
    async def go():
        await setup(rig)
        await rig.manager.pause_rule("r1", actor="a", key="k")
        import os
        other = os.path.join(os.path.dirname(rig.folder), "other")
        os.makedirs(other, exist_ok=True)
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=other), actor="t")
        with pytest.raises(RuleConflict) as err:
            await rig.manager.enable_rule("r1", actor="a")
        assert "code folder" in str(err.value)
        await rig.registry.delete_service("api-gw", actor="t")
        with pytest.raises(RuleConflict):
            await rig.manager.enable_rule("r1", actor="a")

    asyncio.run(go())


def test_global_pause_persists_changes_no_status_and_denies_nothing(tmp_path):
    rig = make_manager("store")

    async def go():
        await setup(rig)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        status = await rig.manager.pause(actor="irc:bob", key="bob")
        assert status.paused and status.paused_by == "irc:bob"
        assert rig.manager.rule("r1").status == "active"
        assert rig.hub.get_proposal(prop.id).status == "approved"
        assert (await rig.store.read(load_state))[0] is True
        again = ApprovalManager.load(rig.store, state=rig.hub, writer=rig.hub, registry=rig.registry,
                                     limiter=rig.limiter, announce=lambda t: asyncio.sleep(0), now=rig.clock)
        assert again.paused is True                              # a restart does not resume
        resumed = await rig.manager.resume(actor="admin")
        assert not resumed.paused and (await rig.store.read(load_state))[0] is False

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_every_reduce_goes_through_the_limiter_and_announces_only_when_allowed(rig):
    async def go():
        await setup(rig, 2)
        await rig.manager.revoke_rule("r1", actor="a", key="key-1")
        rig.limiter.allow = False
        said = len(rig.announced)
        await rig.manager.pause_rule("r2", actor="a", key="key-2")
        await rig.manager.pause(actor="a", key="key-3")
        assert len(rig.announced) == said                        # suppressed by the limiter
        assert [c["key"] for c in rig.limiter.calls] == ["key-1", "key-2", "key-3"]
        assert [c["action"] for c in rig.limiter.calls] == ["rule_revoked", "rule_paused", "rules_paused"]
        assert rig.manager.rule("r2").status == "suspended" and rig.manager.paused   # the action still happened

    asyncio.run(go())


def test_eleven_calls_all_take_effect_with_ten_rows_and_one_coalesced_row():
    rig = make_manager("store")
    real = ReduceAuditLimiter(rig.store.audit, now=rig.clock)
    rig.manager._limiter = real

    async def go():
        await rig.add_service()
        for i in range(11):
            await rig.manager.create_rule(fields(match_value=f"s{i}"), actor="a")
        for i in range(11):
            await rig.manager.revoke_rule(f"r{i + 1}", actor="a", key="same-key")
        assert all(r.status == "revoked" for r in rig.manager.rules())
        await real.aclose()
        actions = await rig.actions()
        assert actions.count("rule_revoked") == 10 and actions.count("reduce_ops_suppressed") == 1

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_a_failing_store_leaves_the_reduce_in_memory_and_the_sweep_repairs_it(rig):
    async def go():
        await setup(rig, 2)
        gate = Gate(rig.store, fail={1, 2, 3})
        revoked = await rig.manager.revoke_rule("r1", actor="a", key="k")       # returns normally
        paused = await rig.manager.pause(actor="a", key="k")
        assert revoked.status == "revoked" and paused.paused
        assert "r1" in rig.manager._book.dirty and rig.manager._book.paused_dirty
        assert (await db_rule(rig, "r1")).status == "active"
        gate.restore()
        await rig.manager.sweep_once()
        assert (await db_rule(rig, "r1")).status == "revoked"
        assert (await rig.store.read(load_state))[0] is True
        assert not rig.manager._book.dirty and not rig.manager._book.paused_dirty

    asyncio.run(go())


# ---- review H2: interleavings of widen and reduce ---------------------------------------------------------

def test_revoke_while_enable_awaits_its_commit(rig):
    async def go():
        await setup(rig)
        await rig.manager.pause_rule("r1", actor="a", key="k")
        gate = Gate(rig.store, hold=1)                       # the enable's write waits before reaching the database
        enabling = asyncio.ensure_future(rig.manager.enable_rule("r1", actor="admin"))
        await asyncio.wait_for(gate.reached.wait(), 5)
        await rig.manager.revoke_rule("r1", actor="a", key="k")          # second transact call, not held
        gate.release.set()
        with pytest.raises(RuleConflict):
            await enabling
        gate.restore()
        assert rig.manager.rule("r1").status == "revoked"
        assert (await db_rule(rig, "r1")).status == "revoked"

    asyncio.run(go())


def test_revoke_whose_persist_failed_survives_an_enable_commit(rig):
    async def go():
        await setup(rig)
        await rig.manager.pause_rule("r1", actor="a", key="k")
        gate = Gate(rig.store, hold=1, after=True, fail={2})       # enable commits; the revoke's persist fails
        enabling = asyncio.ensure_future(rig.manager.enable_rule("r1", actor="admin"))
        await asyncio.wait_for(gate.reached.wait(), 5)
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        assert (await db_rule(rig, "r1")).status == "active"       # the widen's commit is what the database holds
        gate.release.set()
        with pytest.raises(RuleConflict):
            await enabling
        gate.restore()
        assert rig.manager.rule("r1").status == "revoked" and "r1" in rig.manager._book.dirty
        await rig.manager.sweep_once()
        assert (await db_rule(rig, "r1")).status == "revoked"

    asyncio.run(go())


def test_pause_while_resume_awaits(rig):
    async def go():
        await setup(rig)
        await rig.manager.pause(actor="a", key="k")
        gate = Gate(rig.store, hold=1, after=True)
        resuming = asyncio.ensure_future(rig.manager.resume(actor="admin"))
        await asyncio.wait_for(gate.reached.wait(), 5)
        rig.manager._book.set_paused(True, "c", 1.0)                # a reduce lands in the mirror meanwhile
        gate.release.set()
        with pytest.raises(RuleConflict):
            await resuming
        gate.restore()
        assert rig.manager.paused is True and rig.manager._book.paused_dirty
        await rig.manager.sweep_once()
        assert (await rig.store.read(load_state))[0] is True

    asyncio.run(go())


def test_revoke_while_patch_awaits(rig):
    async def go():
        await setup(rig)
        gate = Gate(rig.store, hold=1, after=True)
        patching = asyncio.ensure_future(rig.manager.patch_rule("r1", RulePatch(max_lines=7), actor="admin"))
        await asyncio.wait_for(gate.reached.wait(), 5)
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        gate.release.set()
        with pytest.raises(RuleConflict):
            await patching
        gate.restore()
        assert rig.manager.rule("r1").status == "revoked" and rig.manager.rule("r1").max_lines == 100
        assert (await db_rule(rig, "r1")).status == "revoked"

    asyncio.run(go())


def test_enable_for_a_row_that_is_already_revoked_in_the_database(rig):
    async def go():
        await setup(rig)
        await rig.manager.pause_rule("r1", actor="a", key="k")
        await rig.store.transact(lambda tx: tx.execute("UPDATE approval_rules SET status = 'revoked' WHERE id = 'r1'"))
        with pytest.raises(RuleConflict):
            await rig.manager.enable_rule("r1", actor="a")
        assert rig.manager.rule("r1").status == "suspended"

    asyncio.run(go())


def test_reduce_operations_take_no_lock(rig):
    async def go():
        await setup(rig)
        async with rig.manager._rules_lock:
            await asyncio.wait_for(rig.manager.revoke_rule("r1", actor="a", key="k"), 5)
            await asyncio.wait_for(rig.manager.pause(actor="a", key="k"), 5)
        assert rig.manager.rule("r1").status == "revoked"

    asyncio.run(go())
