"""Service change subscriptions (task 4.5): a folder change or a deleted service suspends the rules (R6.9)."""
from __future__ import annotations

import asyncio
import dataclasses
import os
import threading

import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_reduce import db_rule, fields, rule_approved

from chimericmesh.approvals import rulebook as rb
from chimericmesh.approvals.errors import RuleConflict
from chimericmesh.services import SubscriberFailed
from chimericmesh.services.models import CLEAR, ServicePatch
from chimericmesh.store.audit import AuditLog
from chimericmesh.store.store import Store
from chimericmesh.store.writer import THREAD_NAME

MODES = ["bare", "store"]


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def with_rules(rig, n: int = 2):
    await rig.add_service()
    for i in range(n):
        await rig.manager.create_rule(fields(match_value=f"sig{i}"), actor="a")


def other_folder(rig) -> str:
    path = os.path.join(os.path.dirname(rig.folder), "other")
    os.makedirs(path, exist_ok=True)
    return path


def test_folder_change_suspends_active_rules_denies_proposals_and_announces(rig):
    async def go():
        await with_rules(rig)
        await rig.manager.pause_rule("r2", actor="a", key="k")                  # already suspended: reason stays
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        said = len(rig.announced)
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=other_folder(rig)), actor="t")
        r1 = rig.manager.rule("r1")
        assert r1.status == "suspended" and r1.status_reason == "code_folder_changed"
        assert (await db_rule(rig, "r1")).status_reason == "code_folder_changed"
        assert rig.manager.rule("r2").status_reason.startswith("paused by")
        suspended = [r for r in await rig.audit(rule_id="r1") if r["action"] == "rule_suspended"]
        assert len(suspended) == 1 and suspended[0]["detail"] == "code_folder_changed"
        p = rig.hub.get_proposal(prop.id)
        assert p.status == "denied" and p.decided_by == "rule-suspended:r1"
        assert len([t for t in rig.announced[said:] if "suspended" in t and "r1" in t]) == 1

    asyncio.run(go())


def test_removing_the_folder_does_the_same(rig):
    async def go():
        await with_rules(rig, 1)
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=CLEAR), actor="t")
        assert rig.manager.rule("r1").status == "suspended"

    asyncio.run(go())


def test_deleting_the_service_suspends_its_rules_and_keeps_the_rows(rig):
    async def go():
        await with_rules(rig)
        await rig.manager.pause_rule("r2", actor="a", key="k")
        await rig.registry.delete_service("api-gw", actor="t")
        assert [r.status for r in rig.manager.rules()] == ["suspended", "suspended"]
        assert rig.manager.rule("r1").status_reason == "service_deleted"
        assert rig.manager.rule("r2").status_reason == "service_deleted"
        assert {r.id for r in await rig.store.read(rb.load_rules)} == {"r1", "r2"}
        assert rig.manager.rule("r1").use_count == 0

    asyncio.run(go())


def test_a_change_that_leaves_the_folder_alone_changes_nothing(rig):
    async def go():
        await with_rules(rig)
        await rig.registry.update_service("api-gw", ServicePatch(name="renamed"), actor="t")
        assert all(r.status == "active" for r in rig.manager.rules())
        assert "rule_suspended" not in await rig.actions()

    asyncio.run(go())


def test_a_raising_callback_rolls_the_service_change_back(rig, monkeypatch):
    async def go():
        await with_rules(rig)

        def boom(*a, **k):
            raise RuntimeError("callback failed")

        monkeypatch.setattr(rb, "suspend_service_rules", boom)
        with pytest.raises(SubscriberFailed) as err:
            await rig.registry.update_service("api-gw", ServicePatch(code_folder=other_folder(rig)), actor="t")
        assert err.value.name == "approvals"
        assert rig.registry.get("api-gw").code_folder == rig.folder
        assert all(r.status == "active" for r in await rig.store.read(rb.load_rules))

    asyncio.run(go())


def test_enable_after_a_folder_change_conflicts_and_a_new_rule_can_be_created(rig):
    async def go():
        await with_rules(rig, 1)
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=other_folder(rig)), actor="t")
        with pytest.raises(RuleConflict):
            await rig.manager.enable_rule("r1", actor="a")
        fresh = await rig.manager.create_rule(fields(match_value="again"), actor="a")
        assert fresh.code_folder == other_folder(rig) and fresh.status == "active"

    asyncio.run(go())


def test_callbacks_use_only_their_tx_on_the_writer_thread(rig, monkeypatch):
    async def go():
        await with_rules(rig, 1)
        seen: list[str] = []

        def guard(original, label):
            def wrapper(*a, **k):
                if threading.current_thread().name == THREAD_NAME:
                    seen.append(label)
                    raise AssertionError(f"{label} called from the writer thread")
                return original(*a, **k)
            return wrapper

        async def guard_async(original, label):
            return original

        real_append = AuditLog.append
        real_transact = Store.transact

        async def append(self, *a, **k):
            if threading.current_thread().name == THREAD_NAME:
                seen.append("append")
                raise AssertionError("append from the writer thread")
            return await real_append(self, *a, **k)

        async def transact(self, fn):
            if threading.current_thread().name == THREAD_NAME:
                seen.append("transact")
                raise AssertionError("transact from the writer thread")
            return await real_transact(self, fn)

        monkeypatch.setattr(AuditLog, "append", append)
        monkeypatch.setattr(Store, "transact", transact)
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=other_folder(rig)), actor="t")
        assert seen == [] and rig.manager.rule("r1").status == "suspended"

    asyncio.run(go())


def test_a_skipped_after_commit_is_repaired_by_the_next_sweep(rig):
    async def go():
        await with_rules(rig, 1)
        iid = await open_incident(rig.hub)
        prop = await rule_approved(rig, iid, "r1")
        rig.registry._subs[:] = [dataclasses.replace(s, after_commit=None) for s in rig.registry._subs]
        await rig.registry.update_service("api-gw", ServicePatch(code_folder=other_folder(rig)), actor="t")
        assert rig.manager.rule("r1").status == "active"                        # the mirror has not heard yet
        await rig.manager.sweep_once()
        assert rig.manager.rule("r1").status == "suspended"
        assert rig.hub.get_proposal(prop.id).status == "denied"

    asyncio.run(go())


# ---- review H2 and L1 ---------------------------------------------------------------------------------

def test_a_reload_that_started_before_a_revoke_does_not_move_the_rule_back(rig):
    async def go():
        await with_rules(rig, 1)
        real = rig.store.read
        reached, release = asyncio.Event(), asyncio.Event()

        async def gated(fn):
            result = await real(fn)                       # the read finishes with the old rows...
            reached.set()
            await release.wait()                          # ...and is delivered late
            return result

        rig.store.read = gated
        reload_task = asyncio.ensure_future(rig.manager._reload())
        await asyncio.wait_for(reached.wait(), 5)
        rig.store.read = real
        await rig.manager.revoke_rule("r1", actor="a", key="k")
        release.set()
        await reload_task
        assert rig.manager.rule("r1").status == "revoked"

    asyncio.run(go())


def test_a_reload_that_started_before_an_enable_does_not_undo_it(rig):
    async def go():
        await with_rules(rig, 1)
        await rig.manager.pause_rule("r1", actor="a", key="k")
        real = rig.store.read
        reached, release = asyncio.Event(), asyncio.Event()

        async def gated(fn):
            result = await real(fn)
            reached.set()
            await release.wait()
            return result

        rig.store.read = gated
        reload_task = asyncio.ensure_future(rig.manager._reload())
        await asyncio.wait_for(reached.wait(), 5)
        rig.store.read = real
        await rig.manager.enable_rule("r1", actor="admin")
        release.set()
        await reload_task
        assert rig.manager.rule("r1").status == "active"

    asyncio.run(go())


def test_the_subscriber_failed_name_comes_from_services():
    from chimericmesh import services

    assert services.SubscriberFailed.__name__ == "SubscriberFailed"
