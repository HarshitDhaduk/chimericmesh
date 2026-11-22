"""Creating a rule from a proposal (task 4.3)."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio

import pytest
from approvals_helpers import make_manager, open_incident

from chimericmesh.approvals.errors import RuleInvalid, RuleNotApplicable
from chimericmesh.store import NotFound

MODES = ["bare", "store"]


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


async def approved_proposal(rig, *, severity: int = 3, host: str = "web01", message: str = "disk full on /var"):
    iid = await open_incident(rig.hub, host=host, severity=severity, message=message, count=3 if severity > 2 else 1)
    prop = await rig.hub.create_proposal(iid, "free up space")
    await rig.hub.decide(prop.id, True, "alice")
    return rig.hub.get_proposal(prop.id), rig.hub.get_incident(iid)


def test_defaults_for_an_approved_proposal(rig):
    async def go():
        await rig.add_service()
        prop, inc = await approved_proposal(rig)
        before = rig.manager.rules()
        d = rig.manager.rule_defaults(prop.id)
        assert d.service_id == "api-gw" and d.match_type == "signature" and d.match_value == inc.signature
        assert d.most_urgent_severity == 3 and (d.max_per_hour, d.max_per_incident, d.max_files, d.max_lines) == (3, 1, 5, 100)
        assert d.expires_at == rig.clock.t + 7 * 86400 and d.source_proposal_id == prop.id
        assert rig.manager.rules() == before == []

    asyncio.run(go())


@pytest.mark.parametrize("incident_severity,expected", [(3, 3), (4, 4), (2, 3)])
def test_default_severity_is_clamped_to_3_or_4(rig, incident_severity, expected):
    async def go():
        await rig.add_service()
        prop, inc = await approved_proposal(rig, severity=incident_severity,
                                            message=f"error number one sev {incident_severity}")
        assert rig.manager.rule_defaults(prop.id).most_urgent_severity == expected

    asyncio.run(go())


def test_done_proposal_needs_a_passed_verification(rig):
    async def go():
        await rig.add_service()
        for result, ok in (("passed", True), ("failed", False), ("not_run", False), (None, False)):
            iid = await open_incident(rig.hub, message=f"unique failure {result}")
            prop = await rig.hub.create_proposal(iid, "fix")
            await rig.hub.decide(prop.id, True, "alice")
            extra = {"verification_result": result} if result else None
            await rig.hub.complete(prop.id, "done", extra=extra)
            if ok:
                assert rig.manager.rule_defaults(prop.id).service_id == "api-gw"
            else:
                with pytest.raises(RuleNotApplicable):
                    rig.manager.rule_defaults(prop.id)

    asyncio.run(go())


def test_pending_denied_and_rule_approved_are_not_applicable(rig):
    async def go():
        await rig.add_service()
        iid = await open_incident(rig.hub)
        pending = await rig.hub.create_proposal(iid, "a")
        with pytest.raises(RuleNotApplicable):
            rig.manager.rule_defaults(pending.id)
        await rig.hub.decide(pending.id, False, "alice")
        with pytest.raises(RuleNotApplicable):
            rig.manager.rule_defaults(pending.id)
        iid2 = await open_incident(rig.hub, message="another unique failure")
        ruled = await rig.hub.create_proposal(iid2, "b")
        await rig.hub.hub_transition(ruled.id, "approved", decided_by="rule:r1", approved_by_rule="r1")
        with pytest.raises(RuleNotApplicable):
            rig.manager.rule_defaults(ruled.id)
        with pytest.raises(NotFound):
            rig.manager.rule_defaults("p999")

    asyncio.run(go())


def test_incident_without_service_or_service_without_folder(rig):
    async def go():
        prop, _ = await approved_proposal(rig, host="stranger")
        with pytest.raises(RuleInvalid) as err:
            rig.manager.rule_defaults(prop.id)
        assert err.value.field == "service_id" and "service" in err.value.reason
        await rig.add_service("nofolder", folder=None, hosts=("db01",))
        prop2, _ = await approved_proposal(rig, host="db01", message="other failure here")
        with pytest.raises(RuleInvalid) as err:
            rig.manager.rule_defaults(prop2.id)
        assert "code folder" in err.value.reason

    asyncio.run(go())


def test_create_from_proposal_with_overrides(rig):
    async def go():
        await rig.add_service()
        prop, inc = await approved_proposal(rig)
        rule = await rig.manager.create_rule_from_proposal(
            prop.id, {"match_type": "category", "match_value": "disk", "max_lines": 40, "max_per_hour": 2},
            actor="rest:127.0.0.1")
        assert rule.source_proposal_id == prop.id and rule.match_type == "category" and rule.match_value == "disk"
        assert (rule.max_lines, rule.max_per_hour, rule.max_files) == (40, 2, 5)
        rows = [r for r in await rig.audit(rule_id=rule.id) if r["action"] == "rule_created"]
        assert prop.id in rows[0]["detail"] and rows[0]["proposal_id"] == prop.id

    asyncio.run(go())


def test_create_from_proposal_applies_the_same_checks_and_bounds(rig):
    async def go():
        await rig.add_service()
        iid = await open_incident(rig.hub)
        pending = await rig.hub.create_proposal(iid, "a")
        with pytest.raises(RuleNotApplicable):
            await rig.manager.create_rule_from_proposal(pending.id, {}, actor="a")
        await rig.hub.decide(pending.id, True, "alice")
        for overrides in ({"max_lines": 501}, {"service_id": "other"}, {"most_urgent_severity": 2},
                          {"expires_at": rig.clock.t + 40 * 86400}, {"created_by": "x"}):
            with pytest.raises(RuleInvalid):
                await rig.manager.create_rule_from_proposal(pending.id, overrides, actor="a")
        assert rig.manager.rules() == []

    asyncio.run(go())
