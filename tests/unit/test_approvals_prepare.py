"""``prepare``, ``confirm``, ``abort`` and ``line_for`` (task 5.1).

The manager is built from the named interfaces only (a state view, a proposal writer, the registry, a limiter,
an announce function and a clock). This module never imports ``chimericmesh.hub`` (review M2).
"""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import dataclasses
import inspect
import os
import tempfile

import pytest
from approvals_helpers import FakeClock, FakeLimiter

from chimericmesh.approvals import manager as manager_module
from chimericmesh.approvals.errors import ActiveProposalExists, ProposalInvalid, ProposalRateLimited
from chimericmesh.approvals.manager import ApprovalManager
from chimericmesh.approvals.models import CreateContext, RuleFields, ApprovalSettings
from chimericmesh.approvals.schema import approvals_registry
from chimericmesh.services.models import ServiceFields
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store
from chimericmesh.store.models import Proposal

assert "chimericmesh.hub" not in inspect.getsource(manager_module)


class FakeState:
    """A StateView over a dict of proposals; incidents are not needed by ``prepare``."""

    def __init__(self, clock):
        self.clock = clock
        self.props: dict[str, Proposal] = {}

    def now(self):
        return self.clock()

    def get_incident(self, iid):
        return None

    def incidents(self, **kw):
        return []

    def get_proposal(self, pid):
        return self.props.get(pid)

    def list_proposals(self, status=None, *, service_id=None, limit=None):
        return [p for p in self.props.values() if status in (None, p.status)]


class Rig:
    def __init__(self, cooldown: float = 86400.0):
        self.clock = FakeClock()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.realpath(os.path.join(self.tmp.name, "code"))
        os.makedirs(self.folder)
        self.store = Store.open(":memory:", registry=approvals_registry())
        self.state = FakeState(self.clock)
        self.registry = ServiceRegistry.load(self.store, state=self.state, clock=self.clock)
        self.announced: list[str] = []

        async def announce(text):
            self.announced.append(text)

        self.limiter = FakeLimiter()
        self.manager = ApprovalManager.load(
            self.store, state=self.state, writer=None, registry=self.registry, limiter=self.limiter,
            announce=announce, settings=ApprovalSettings(autofix_cooldown=cooldown), now=self.clock)

    async def seed(self, n_rules: int = 1, **rule_kw):
        await self.registry.create_service(ServiceFields(id="api-gw", name="api-gw", hosts=("web01",),
                                                         code_folder=self.folder), actor="t")
        for i in range(n_rules):
            await self.manager.create_rule(RuleFields(
                service_id="api-gw", match_type="signature", match_value="disk full on /var",
                **{"max_per_hour": 3, **rule_kw}), actor="a")

    def ctx(self, **kw) -> CreateContext:
        extra = {"kind": "code_change", "files": ["a.py"], "estimated_lines": 10, "verification": "run tests"}
        extra.update(kw.pop("extra", {}))
        base = dict(incident_id="i1", service_id="api-gw", signature="disk full on /var", category="disk",
                    severity=3, opening=1, action="free space", details="", extra=extra, enforce_limits=False)
        base.update(kw)
        return CreateContext(**base)

    def prop(self, pid="p1", **kw) -> Proposal:
        return Proposal(id=pid, incident_id="i1", action="free space", service_id="api-gw", incident_opening=1,
                        created=self.clock.t, **kw)

    def close(self):
        self.store.close()
        self.tmp.cleanup()


@pytest.fixture()
def rig():
    r = Rig()
    yield r
    r.close()


def test_a_matching_context_is_auto_with_the_remaining_quota(rig):
    async def go():
        await rig.seed(max_per_hour=3)
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "auto" and plan.rule_id == "r1"
        assert plan.remaining_hour == 2 and plan.remaining_incident == 0
        assert [(c.rule_id, c.failed) for c in plan.checks] == [("r1", None)]
        assert plan.extra["files"] == ["a.py"] and plan.files == ("a.py",)

    asyncio.run(go())


def test_file_outside_folder_or_too_many_lines_stays_pending_with_the_failed_condition(rig):
    async def go():
        await rig.seed()
        plan = rig.manager.prepare(rig.ctx(extra={"files": [".git/hooks/pre-commit.py"]}))   # protected, still inside
        assert plan.kind == "pending" and [(c.rule_id, c.failed) for c in plan.checks] == [("r1", "files")]
        assert rig.manager.prepare(rig.ctx(extra={"estimated_lines": 101})).checks[0].failed == "lines"
        assert rig.manager.prepare(rig.ctx(extra={"verification": ""})).checks[0].failed == "verification"
        assert rig.manager.prepare(rig.ctx(extra={"files": ["conf.json"]})).checks[0].failed == "files"

    asyncio.run(go())


def test_two_rules_the_second_matches(rig):
    async def go():
        await rig.seed(0)
        await rig.manager.create_rule(RuleFields("api-gw", "signature", "other"), actor="a")
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk"), actor="a")
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "auto" and plan.rule_id == "r2"
        assert [(c.rule_id, c.failed) for c in plan.checks] == [("r1", "match"), ("r2", None)]

    asyncio.run(go())


def test_no_rules_means_pending_without_checks_and_without_audit_work(rig):
    async def go():
        await rig.seed(0)
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "pending" and plan.checks == () and not plan.paused_skip

    asyncio.run(go())


def test_paused_hub_is_pending_and_never_matches(rig):
    async def go():
        await rig.seed(2)
        await rig.manager.pause(actor="a", key="k")
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "pending" and plan.checks == () and plan.paused_skip is True

    asyncio.run(go())


def test_never_covered_cases(rig):
    async def go():
        await rig.seed()
        assert rig.manager.prepare(rig.ctx(service_id=None)).kind == "pending"
        assert rig.manager.prepare(rig.ctx(severity=2)).checks[0].failed == "severity"
        assert rig.manager.prepare(rig.ctx(extra={"kind": None})).checks[0].failed == "kind"

    asyncio.run(go())


def test_invalid_input_raises_and_reserves_nothing(rig):
    async def go():
        await rig.seed()
        before = (dict(rig.manager._ledger._uses), dict(rig.manager._limits._tokens))
        for extra in ({"kind": "config_change"}, {"files": ["/etc/passwd.py"]}, {"files": ["a\x00.py"]},
                      {"estimated_lines": 5000}, {"files": ["a.py"] * 21}):
            with pytest.raises(ProposalInvalid):
                rig.manager.prepare(rig.ctx(extra=extra, enforce_limits=True))
        assert (dict(rig.manager._ledger._uses), dict(rig.manager._limits._tokens)) == before

    asyncio.run(go())


def test_limits_on_block_a_second_proposal_and_the_eleventh_in_an_hour(rig):
    async def go():
        await rig.seed()
        plan = rig.manager.prepare(rig.ctx(enforce_limits=True))
        with pytest.raises(ActiveProposalExists):
            rig.manager.prepare(rig.ctx(enforce_limits=True))
        rig.manager.abort(plan)
        rig.manager.prepare(rig.ctx(enforce_limits=True))
        for n in range(9):
            rig.manager.prepare(rig.ctx(incident_id=f"x{n}", enforce_limits=True))
        with pytest.raises(ProposalRateLimited):
            rig.manager.prepare(rig.ctx(incident_id="x99", enforce_limits=True))

    asyncio.run(go())


def test_limits_off_never_raise(rig):
    async def go():
        await rig.seed(0)
        for _ in range(30):
            rig.manager.prepare(rig.ctx())

    asyncio.run(go())


def test_abort_releases_and_confirm_binds(rig):
    async def go():
        await rig.seed(max_per_hour=1)
        plan = rig.manager.prepare(rig.ctx(enforce_limits=True))
        assert rig.manager.prepare(rig.ctx(incident_id="i2")).kind == "pending"        # the one slot is reserved
        rig.manager.abort(plan)
        again = rig.manager.prepare(rig.ctx(enforce_limits=True))
        assert again.kind == "auto"
        assert rig.manager.confirm(again, rig.prop()) is None
        assert rig.manager._ledger.approved_at("p1") == rig.clock.t

    asyncio.run(go())


def test_prepare_is_synchronous_and_two_calls_share_one_quota_slot(rig):
    async def go():
        await rig.seed(max_per_hour=1)
        assert not inspect.iscoroutinefunction(rig.manager.prepare)
        first = rig.manager.prepare(rig.ctx(incident_id="a"))
        second = rig.manager.prepare(rig.ctx(incident_id="b"))
        assert sorted([first.kind, second.kind]) == ["auto", "pending"]

    asyncio.run(go())


def test_an_error_in_matching_is_a_refusal_never_an_approval(rig, monkeypatch):
    async def go():
        await rig.seed()
        from chimericmesh.approvals import matching

        def broken(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(matching, "match_rule", broken)
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "pending" and plan.checks[0].failed == "error" and plan.token == 0

    asyncio.run(go())


# ---- cooldown: the one formula (review H1) -------------------------------------------------------------------

def _completed_fix(rig, at):
    """A use of rule r1 for incident i1 opening 1 that was completed at ``at``."""
    plan = rig.manager.prepare(rig.ctx(opening=1))
    rig.manager.confirm(plan, rig.prop("p1"))
    rig.manager._ledger.completed("p1", at)
    return plan


H = 3600.0


def test_reopen_inside_the_cooldown_needs_the_user_and_stays_so(rig):
    async def go():
        await rig.seed()
        _completed_fix(rig, rig.clock.t)
        rig.clock.advance(H)
        plan = rig.manager.prepare(rig.ctx(opening=2))
        assert plan.kind == "cooldown" and plan.prior_proposal_id == "p1" and plan.rule_id == "r1"
        assert plan.opening_first_seen == rig.clock.t
        rig.clock.advance(29 * H)
        again = rig.manager.prepare(rig.ctx(opening=2))
        assert again.kind == "cooldown" and again.opening_first_seen is None      # same opening, still waiting

    asyncio.run(go())


def test_reopen_after_the_cooldown_is_auto_and_exactly_at_it_too(rig):
    async def go():
        await rig.seed()
        _completed_fix(rig, rig.clock.t)
        rig.clock.advance(25 * H)
        assert rig.manager.prepare(rig.ctx(opening=2)).kind == "auto"

    asyncio.run(go())
    rig2 = Rig()

    async def go2():
        await rig2.seed()
        _completed_fix(rig2, rig2.clock.t)
        rig2.clock.advance(86400)
        assert rig2.manager.prepare(rig2.ctx(opening=2)).kind == "auto"

    try:
        asyncio.run(go2())
    finally:
        rig2.close()


def test_the_first_sighting_is_written_once(rig):
    async def go():
        await rig.seed()
        _completed_fix(rig, rig.clock.t)
        rig.clock.advance(H)
        plan = rig.manager.prepare(rig.ctx(opening=2))
        await rig.store.transact(lambda tx: plan.write_in_tx(tx, rig.prop("p2")))
        rig.clock.advance(H)
        later = rig.manager.prepare(rig.ctx(opening=2))
        await rig.store.transact(lambda tx: later.write_in_tx(tx, rig.prop("p3")))
        rows = await rig.store.read(lambda tx: [tuple(r) for r in tx.all("SELECT * FROM incident_openings")])
        assert rows == [("i1", 2, plan.opening_first_seen)]
        actions = [r["action"] for r in await rig.store.audit.read(50)]
        assert actions.count("autofix_cooldown") == 2

    asyncio.run(go())


# ---- confirm and line_for --------------------------------------------------------------------------------

def test_confirm_returns_none_for_pending_cooldown_and_a_still_active_rule(rig):
    async def go():
        await rig.seed()
        auto = rig.manager.prepare(rig.ctx())
        assert rig.manager.confirm(auto, rig.prop("p1")) is None
        pending = rig.manager.prepare(rig.ctx(incident_id="i2", extra={"estimated_lines": 999}))
        assert rig.manager.confirm(pending, rig.prop("p2")) is None

    asyncio.run(go())


def test_confirm_returns_a_denial_when_the_rule_changed_during_the_commit(rig):
    async def go():
        await rig.seed()
        plan = rig.manager.prepare(rig.ctx())
        rule = rig.manager.rule("r1")
        rig.manager._book.replace(dataclasses.replace(rule, status="revoked"))
        assert rig.manager.confirm(plan, rig.prop("p1")) == "rule-revoked:r1"
        plan = rig.manager.prepare(rig.ctx(incident_id="i2"))
        assert plan.kind == "pending"                                               # revoked rules never match
        rig.manager._book.replace(dataclasses.replace(rule, status="active"))
        plan = rig.manager.prepare(rig.ctx(incident_id="i3"))
        rig.manager._book.replace(dataclasses.replace(rule, status="suspended"))
        assert rig.manager.confirm(plan, rig.prop("p3")) == "rule-suspended:r1"
        rig.manager._book.replace(dataclasses.replace(rule, status="active"))
        plan = rig.manager.prepare(rig.ctx(incident_id="i4"))
        rig.clock.advance(8 * 86400)
        assert rig.manager.confirm(plan, rig.prop("p4")) == "rule-expired:r1"

    asyncio.run(go())


def test_line_for_each_kind(rig):
    async def go():
        await rig.seed()
        base = "PROPOSAL p1 for i1: free space - reply !approve p1 or !deny p1"
        auto = rig.manager.prepare(rig.ctx())
        prop = rig.prop("p1", status="approved", decided_by="rule:r1", approved_by_rule="r1")
        line = rig.manager.line_for(auto, base, prop)
        assert line.startswith("AUTO-APPROVED p1 by rule r1 (service api-gw): free space") and "reply" not in line
        denied = rig.prop("p1", status="denied", decided_by="rule-revoked:r1", approved_by_rule="r1")
        text = rig.manager.line_for(auto, base, denied)
        assert text == "PROPOSAL p1 denied by hub: rule r1 was revoked" and "AUTO-APPROVED" not in text
        pending = rig.manager.prepare(rig.ctx(incident_id="i2", extra={"estimated_lines": 999}))
        assert rig.manager.line_for(pending, base, rig.prop("p2")) == base

    asyncio.run(go())


def test_cooldown_line_keeps_the_baseline_and_adds_the_note(rig):
    async def go():
        await rig.seed()
        _completed_fix(rig, rig.clock.t)
        rig.clock.advance(H)
        plan = rig.manager.prepare(rig.ctx(opening=2))
        base = "PROPOSAL p2 for i1: free space - reply !approve p2 or !deny p2"
        line = rig.manager.line_for(plan, base, rig.prop("p2"))
        assert line.startswith(base) and "p1" in line[len(base):]

    asyncio.run(go())


# ---- precision of what is checked (L7, L10) ----------------------------------------------------------------

def test_no_scope_check_without_an_active_rule_and_one_with(rig, monkeypatch):
    async def go():
        await rig.seed(0)
        calls = []
        real = manager_module.check_files

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        monkeypatch.setattr(manager_module, "check_files", spy)
        rig.manager.prepare(rig.ctx())
        assert calls == []
        await rig.manager.create_rule(RuleFields("api-gw", "signature", "disk full on /var"), actor="a")
        rig.manager.prepare(rig.ctx())
        assert calls == [1]

    asyncio.run(go())


def test_paused_writes_one_skipped_row_not_one_per_rule(rig):
    async def go():
        await rig.seed(2)
        await rig.manager.pause(actor="a", key="k")
        plan = rig.manager.prepare(rig.ctx())
        await rig.store.transact(lambda tx: plan.write_in_tx(tx, rig.prop("p1")))
        rows = [r for r in await rig.store.audit.read(50) if r["action"] == "auto_approval_skipped"]
        assert len(rows) == 1 and rows[0]["detail"] == "condition=paused" and rows[0]["rule_id"] is None

    asyncio.run(go())


def test_no_active_rule_and_paused_writes_no_row():
    rig = Rig()

    async def go():
        await rig.seed(0)
        await rig.manager.pause(actor="a", key="k")
        plan = rig.manager.prepare(rig.ctx())
        await rig.store.transact(lambda tx: plan.write_in_tx(tx, rig.prop("p1")))
        assert [r for r in await rig.store.audit.read(50) if r["action"] == "auto_approval_skipped"] == []

    try:
        asyncio.run(go())
    finally:
        rig.close()


def test_each_checked_rule_gets_a_skipped_row_with_its_first_failing_condition(rig):
    async def go():
        await rig.seed(0)
        await rig.manager.create_rule(RuleFields("api-gw", "signature", "other"), actor="a")
        await rig.manager.create_rule(RuleFields("api-gw", "signature", "disk full on /var", max_lines=5), actor="a")
        plan = rig.manager.prepare(rig.ctx())
        assert plan.kind == "pending"
        await rig.store.transact(lambda tx: plan.write_in_tx(tx, rig.prop("p1")))
        rows = [(r["rule_id"], r["detail"]) for r in await rig.store.audit.read(50)
                if r["action"] == "auto_approval_skipped"]
        assert sorted(rows) == [("r1", "condition=match"), ("r2", "condition=lines")]

    asyncio.run(go())


def test_the_auto_record_has_one_use_one_count_and_one_audit_row(rig):
    async def go():
        await rig.seed()
        plan = rig.manager.prepare(rig.ctx())
        await rig.store.transact(lambda tx: plan.write_in_tx(tx, rig.prop("p1")))
        uses = await rig.store.read(lambda tx: [tuple(r) for r in tx.all(
            "SELECT rule_id, proposal_id, incident_id, opening FROM rule_uses")])
        assert uses == [("r1", "p1", "i1", 1)]
        count = await rig.store.read(lambda tx: tx.one("SELECT use_count FROM approval_rules")[0])
        assert count == 1
        row = [r for r in await rig.store.audit.read(50) if r["action"] == "proposal_auto_approved"][0]
        assert row["actor"] == "rule:r1" and row["proposal_id"] == "p1" and "files=a.py" in row["detail"]
        assert "remaining_hour=2" in row["detail"]

    asyncio.run(go())
