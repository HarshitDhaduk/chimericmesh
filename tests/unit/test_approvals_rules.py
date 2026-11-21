"""Rule lifecycle: create, patch and read (task 4.1). Runs over a bare hub and a store-backed hub."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio

import pytest
from approvals_helpers import FakeClock, make_manager

from chimericmesh.approvals.errors import (
    ApprovalsNotMigrated, RuleConflict, RuleInvalid, RuleLimitReached, RuleNotFound,
)
from chimericmesh.approvals.manager import ApprovalManager
from chimericmesh.approvals.models import MAX_EXPIRY_SECONDS, RuleFields, RulePatch
from chimericmesh.store import Store, StoreError
from chimericmesh.store.schema import core_registry

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


def test_create_rule_stores_and_audits_and_announces(rig):
    async def go():
        await rig.add_service()
        rule = await rig.manager.create_rule(fields(), actor="rest:127.0.0.1")
        assert rule.id == "r1" and rule.status == "active" and rule.created_by == "rest:127.0.0.1"
        assert rule.code_folder == rig.folder and rule.use_count == 0 and rule.status_reason == ""
        assert rule.expires_at == rig.clock.t + 7 * 86400
        assert rig.manager.rule("r1") == rule
        rows = await rig.audit(rule_id="r1")
        assert [r["action"] for r in rows] == ["rule_created"]
        assert rows[0]["actor"] == "rest:127.0.0.1" and rows[0]["service_id"] == "api-gw"
        assert len(rig.announced) == 1 and "r1" in rig.announced[0] and "rest:127.0.0.1" in rig.announced[0]
        # the row really is in the database, not only in the mirror
        from chimericmesh.approvals.rulebook import load_rules
        assert [r.id for r in await rig.store.read(load_rules)] == ["r1"]

    asyncio.run(go())


def test_create_needs_a_known_service_with_a_code_folder(rig):
    async def go():
        with pytest.raises(RuleInvalid) as err:
            await rig.manager.create_rule(fields(service_id="nope"), actor="a")
        assert err.value.field == "service_id"
        await rig.add_service("nofolder", folder=None)
        with pytest.raises(RuleInvalid) as err:
            await rig.manager.create_rule(fields(service_id="nofolder"), actor="a")
        assert err.value.field == "service_id" and "code folder" in err.value.reason
        assert rig.manager.rules() == []

    asyncio.run(go())


def test_rule_count_limits_ignore_final_rules(rig):
    async def go():
        await rig.add_service()
        await rig.add_service("other")
        for n in range(20):
            await rig.manager.create_rule(fields(match_value=f"sig {n}"), actor="a")
        with pytest.raises(RuleLimitReached):
            await rig.manager.create_rule(fields(match_value="one more"), actor="a")
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        await rig.manager.create_rule(fields(match_value="fits now"), actor="a")  # a revoked rule does not count
        assert len(rig.manager.rules(service_id="api-gw")) == 21

    asyncio.run(go())


def test_total_rule_limit(rig):
    async def go():
        for n in range(5):
            await rig.add_service(f"svc{n}")
        count = 0
        for n in range(5):
            for m in range(20):
                await rig.manager.create_rule(fields(service_id=f"svc{n}", match_value=f"s{m}"), actor="a")
                count += 1
        assert count == 100
        await rig.add_service("svc5")
        with pytest.raises(RuleLimitReached):
            await rig.manager.create_rule(fields(service_id="svc5"), actor="a")

    asyncio.run(go())


def test_patch_changes_only_numbers_expiry_and_note_with_one_audit_row(rig):
    async def go():
        await rig.add_service()
        rule = await rig.manager.create_rule(fields(), actor="a")
        patched = await rig.manager.patch_rule("r1", RulePatch(max_lines=50, max_files=2, note="n"), actor="bob")
        assert (patched.max_lines, patched.max_files, patched.note) == (50, 2, "n")
        assert patched.match_value == rule.match_value and patched.service_id == rule.service_id
        rows = [r for r in await rig.audit(rule_id="r1") if r["action"] == "rule_updated"]
        assert len(rows) == 1 and rows[0]["actor"] == "bob"
        assert "max_lines: 100 -> 50" in rows[0]["detail"] and "max_files: 5 -> 2" in rows[0]["detail"]
        assert rig.manager.rule("r1") == patched

    asyncio.run(go())


def test_patch_noop_writes_nothing_and_bounds_are_checked(rig):
    async def go():
        await rig.add_service()
        rule = await rig.manager.create_rule(fields(), actor="a")
        before = len(await rig.audit())
        same = await rig.manager.patch_rule("r1", RulePatch(max_lines=rule.max_lines), actor="a")
        assert same == rule and len(await rig.audit()) == before
        with pytest.raises(RuleInvalid):
            await rig.manager.patch_rule("r1", RulePatch(max_lines=501), actor="a")
        with pytest.raises(RuleInvalid):
            await rig.manager.patch_rule("r1", RulePatch(expires_at=rule.created_at + MAX_EXPIRY_SECONDS + 5),
                                         actor="a")
        ok = await rig.manager.patch_rule("r1", RulePatch(expires_at=rule.created_at + MAX_EXPIRY_SECONDS),
                                          actor="a")
        assert ok.expires_at == rule.created_at + MAX_EXPIRY_SECONDS
        with pytest.raises(RuleNotFound):
            await rig.manager.patch_rule("r9", RulePatch(max_lines=5), actor="a")

    asyncio.run(go())


def test_patch_refuses_revoked_and_expired_rules(rig):
    async def go():
        await rig.add_service()
        await rig.manager.create_rule(fields(), actor="a")
        await rig.manager.create_rule(fields(match_value="b"), actor="a")
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        with pytest.raises(RuleConflict):
            await rig.manager.patch_rule("r1", RulePatch(max_lines=5), actor="a")
        rig.clock.advance(8 * 86400)
        with pytest.raises(RuleConflict):
            await rig.manager.patch_rule("r2", RulePatch(max_lines=5), actor="a")

    asyncio.run(go())


def test_reads_filter_and_order(rig):
    async def go():
        await rig.add_service()
        await rig.add_service("other")
        await rig.manager.create_rule(fields(), actor="a")
        await rig.manager.create_rule(fields(service_id="other"), actor="a")
        await rig.manager.create_rule(fields(match_value="x"), actor="a")
        await rig.manager.revoke_rule("r3", actor="a", key="k")
        assert [r.id for r in rig.manager.rules()] == ["r1", "r2", "r3"]
        assert [r.id for r in rig.manager.rules(service_id="api-gw")] == ["r1", "r3"]
        assert [r.id for r in rig.manager.rules(status="revoked")] == ["r3"]
        assert rig.manager.rule("r2").service_id == "other" and rig.manager.rule("nope") is None

    asyncio.run(go())


def test_failing_store_leaves_the_mirror_unchanged(rig):
    async def go():
        await rig.add_service()
        rule = await rig.manager.create_rule(fields(), actor="a")
        real = rig.store.transact

        async def broken(fn):
            raise StoreError("down")

        rig.store.transact = broken
        with pytest.raises(StoreError):
            await rig.manager.create_rule(fields(match_value="two"), actor="a")
        with pytest.raises(StoreError):
            await rig.manager.patch_rule("r1", RulePatch(max_lines=7), actor="a")
        rig.store.transact = real
        assert rig.manager.rules() == [rule]

    asyncio.run(go())


def test_cancelling_the_caller_cannot_split_commit_and_mirror(rig):
    async def go():
        await rig.add_service()
        gate = asyncio.Event()
        real = rig.store.transact

        async def slow(fn):
            await gate.wait()
            return await real(fn)

        rig.store.transact = slow
        task = asyncio.ensure_future(rig.manager.create_rule(fields(), actor="a"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0)
        gate.set()
        await asyncio.sleep(0.3)
        rig.store.transact = real
        from chimericmesh.approvals.rulebook import load_rules
        stored = await rig.store.read(load_rules)
        assert [r.id for r in rig.manager.rules()] == [r.id for r in stored]
        assert len(stored) == 1                       # the unit finished although its caller was cancelled
        assert len(rig.announced) == 1

    asyncio.run(go())


def test_canaries_never_reach_audit_rows_or_announcements(rig):
    async def go():
        await rig.add_service()
        canary = "CANARY-SIGNATURE-4711"
        await rig.manager.create_rule(fields(match_value=canary, note="note\r\nCANARY-NOTE"), actor="a")
        await rig.manager.patch_rule("r1", RulePatch(max_lines=9), actor="a")
        text = " ".join(str(v) for row in await rig.audit() for v in row.values()) + " ".join(rig.announced)
        assert canary not in text and "CANARY-NOTE" not in text
        assert not any(c in line for line in rig.announced for c in "\r\n\x00")

    asyncio.run(go())


# ---- load, ids across restarts, subscription, seeding -----------------------------------------------------

def test_load_subscribes_once_with_the_name_approvals(rig):
    assert [s.name for s in rig.registry._subs] == ["approvals"]


def test_load_on_a_store_without_the_migrations_raises():
    store = Store.open(":memory:", registry=core_registry())
    try:
        rig = make_manager("bare")
        try:
            with pytest.raises(ApprovalsNotMigrated):
                ApprovalManager.load(store, state=rig.hub, writer=rig.hub, registry=rig.registry,
                                     limiter=rig.limiter, announce=lambda t: asyncio.sleep(0))
        finally:
            rig.close()
    finally:
        store.close()


def test_ids_continue_across_managers_and_are_not_reused():
    rig = make_manager("store")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(fields(match_value="a"), actor="x")
        await rig.manager.create_rule(fields(match_value="b"), actor="x")
        again = ApprovalManager.load(rig.store, state=rig.hub, writer=rig.hub, registry=rig.registry,
                                     limiter=rig.limiter, announce=lambda t: asyncio.sleep(0), now=rig.clock)
        assert [r.id for r in again.rules()] == ["r1", "r2"]
        r3 = await again.create_rule(fields(match_value="c"), actor="x")
        assert r3.id == "r3"
        await rig.store.transact(lambda tx: tx.execute("DELETE FROM approval_rules WHERE id = 'r3'"))
        third = ApprovalManager.load(rig.store, state=rig.hub, writer=rig.hub, registry=rig.registry,
                                     limiter=rig.limiter, announce=lambda t: asyncio.sleep(0), now=rig.clock)
        assert (await third.create_rule(fields(match_value="d"), actor="x")).id == "r4"

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_concurrent_creates_get_distinct_ids(rig):
    async def go():
        await rig.add_service()
        made = await asyncio.gather(*(rig.manager.create_rule(fields(match_value=f"s{n}"), actor="a")
                                      for n in range(6)))
        assert len({r.id for r in made}) == 6

    asyncio.run(go())


def test_load_seeds_the_hourly_window_from_recent_proposals(rig):
    from approvals_helpers import open_incident

    async def go():
        await rig.add_service()
        iid = await open_incident(rig.hub)
        for _ in range(10):
            await rig.hub.create_proposal(iid, "fix", "")

        def fresh():
            return ApprovalManager.load(rig.store, state=rig.hub, writer=rig.hub, registry=rig.registry,
                                        limiter=rig.limiter, announce=lambda t: asyncio.sleep(0), now=rig.clock)

        rig.clock.advance(1800)
        assert fresh()._limits.count("api-gw", rig.clock.t) == 10
        rig.clock.advance(1801)                       # now more than an hour after the proposals
        assert fresh()._limits.count("api-gw", rig.clock.t) == 0
    asyncio.run(go())
