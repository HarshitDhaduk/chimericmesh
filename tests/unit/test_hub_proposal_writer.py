"""The proposal write seam on a bare Hub (ProposalWriter / ProposalClient)."""
import asyncio

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.ports import ProposalClient, ProposalWriter

MSG = "could not write block 1 No space left on device"
EXTRA = {"kind": "edit", "files": ["a.py"], "estimated_lines": 5, "verification": "pytest -q"}
BASELINE = {"id", "incident_id", "action", "details", "status", "created", "decided_by", "decided_at",
            "result_note"}


async def make():
    hub = Hub()
    for _ in range(3):
        await hub.ingest(Event(source="syslog", host="db01", app="pg", severity=3, message=MSG))
    return hub, hub.incidents()[0]


def test_create_with_extra_carries_the_attributes():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix", extra=EXTRA)
        assert (prop.kind, prop.files, prop.estimated_lines, prop.verification) == ("edit", ["a.py"], 5, "pytest -q")
        assert set(prop.to_dict()) == BASELINE
        assert EXTRA.items() <= prop.to_full_dict().items()

    asyncio.run(go())


def test_unknown_extra_key_raises_and_creates_nothing():
    async def go():
        hub, inc = await make()
        with pytest.raises(ValueError):
            await hub.create_proposal(inc.id, "fix", extra={"nope": 1})
        assert hub.list_proposals() == []
        assert (await hub.create_proposal(inc.id, "fix")).id == "p1"  # the counter did not advance

    asyncio.run(go())


def test_hub_transition_stores_hub_origin_and_rule():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix")
        got = await hub.hub_transition(prop.id, "approved", decided_by="rule:r1", approved_by_rule="r1")
        assert got is prop
        assert (prop.status, prop.decision_origin, prop.approved_by_rule, prop.decided_by) == (
            "approved", "hub", "r1", "rule:r1")

    asyncio.run(go())


def test_hub_can_revoke_an_approval_but_a_plain_name_cannot():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix")
        await hub.decide(prop.id, True, "alice")
        assert await hub.hub_transition(prop.id, "denied", decided_by="alice") is None
        assert prop.status == "approved" and prop.decision_origin == "user"
        got = await hub.hub_transition(prop.id, "denied", decided_by="rule-revoked:r1")
        assert got is prop and prop.status == "denied" and prop.decision_origin == "hub"

    asyncio.run(go())


def test_hub_transition_outside_the_restricted_set_is_rejected():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix")
        before = (prop.status, prop.decided_by, prop.decision_origin)
        # pending -> approved needs approved_by_rule; pending -> denied needs withdrawn:/expired:
        assert await hub.hub_transition(prop.id, "approved", decided_by="rule:r1") is None
        assert await hub.hub_transition(prop.id, "denied", decided_by="alice") is None
        assert await hub.hub_transition(prop.id, "done", decided_by="rule:r1") is None
        assert await hub.hub_transition("p99", "denied", decided_by="expired:x") is None
        assert (prop.status, prop.decided_by, prop.decision_origin) == before
        assert (await hub.hub_transition(prop.id, "denied", decided_by="withdrawn:x")).status == "denied"
        prop2 = await hub.create_proposal(inc.id, "fix 2")
        assert (await hub.hub_transition(prop2.id, "denied", decided_by="expired:t")).decision_origin == "hub"

    asyncio.run(go())


def test_user_decide_with_a_reserved_looking_name_is_still_user():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix")
        await hub.decide(prop.id, True, "rule-x")
        assert prop.decision_origin == "user"

    asyncio.run(go())


def test_complete_stores_the_result_extras():
    async def go():
        hub, inc = await make()
        prop = await hub.create_proposal(inc.id, "fix")
        await hub.decide(prop.id, True, "alice")
        got = await hub.complete(prop.id, "n", extra={"files_changed": ["a.py"], "lines_changed": 3,
                                                       "verification_result": "pass"})
        assert got.files_changed == ["a.py"] and got.lines_changed == 3 and got.verification_result == "pass"
        with pytest.raises(ValueError):
            await hub.complete(prop.id, "n", extra={"bogus": 1})

    asyncio.run(go())


def test_hub_implements_the_protocols():
    hub = Hub()
    assert isinstance(hub, ProposalWriter) and isinstance(hub, ProposalClient)
