"""Proposal rules on a bare Hub: the shared transition table, origin and caps."""
import asyncio
import itertools

import pytest

from chimericmesh.api import DecideRequest
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.errors import InvalidTransition
from chimericmesh.store.proposals import PROPOSAL_STATUSES, check_transition

MSG = "could not write block 1 No space left on device"
BASELINE = {"id", "incident_id", "action", "details", "status", "created", "decided_by", "decided_at",
            "result_note"}


async def hub_with_incident(service="svc1"):
    hub = Hub()
    for _ in range(3):
        await hub.ingest(Event(source="syslog", host="db01", app="pg", severity=3, message=MSG,
                               service_id=service))
    return hub, hub.incidents()[0]


def allowed(old, new, decided_by="alice"):
    try:
        check_transition(old, new, origin="user", decided_by=decided_by, authority=None)
    except InvalidTransition:
        return False
    return True


@pytest.mark.parametrize("old,new", list(itertools.product(PROPOSAL_STATUSES, PROPOSAL_STATUSES)))
def test_every_status_pair_follows_the_table(old, new):
    async def go():
        hub, inc = await hub_with_incident()
        prop = await hub.create_proposal(inc.id, "fix")
        prop.status = old
        before = (prop.status, prop.decided_by, prop.decided_at, prop.decision_origin, prop.result_note)
        if new in ("approved", "denied"):
            result = await hub.decide(prop.id, new == "approved", "alice")
        elif new == "done":
            result = await hub.complete(prop.id, "ok")
        else:
            return  # no hub method moves a proposal back to pending
        if allowed(old, new):
            assert result is prop and prop.status == new
        else:
            assert result is None
            assert (prop.status, prop.decided_by, prop.decided_at, prop.decision_origin, prop.result_note) == before

    asyncio.run(go())


def test_decide_and_complete_set_user_origin():
    async def go():
        hub, inc = await hub_with_incident()
        prop = await hub.create_proposal(inc.id, "fix")
        assert prop.decision_origin is None
        await hub.decide(prop.id, True, "alice")
        assert prop.decision_origin == "user"
        await hub.complete(prop.id, "done")
        assert prop.decision_origin == "user" and prop.status == "done"

    asyncio.run(go())


@pytest.mark.parametrize("by", ["rule:r1", "rule-revoked:r1", "withdrawn:x", "expired:x", "expired-1", "RULE:x"])
def test_reserved_decided_by_cannot_produce_hub_origin(by):
    async def go():
        hub, inc = await hub_with_incident()
        prop = await hub.create_proposal(inc.id, "fix")
        await hub.decide(prop.id, True, by)
        assert prop.decision_origin == "user"
        # approved -> denied is reserved for the hub's own rule logic, whatever decided_by says
        assert await hub.decide(prop.id, False, "rule-revoked:r1") is None
        assert prop.status == "approved"

    asyncio.run(go())


def test_decided_by_keeps_60_characters_but_the_rest_model_stays_at_30():
    async def go():
        hub, inc = await hub_with_incident()
        prop = await hub.create_proposal(inc.id, "fix")
        await hub.decide(prop.id, True, "n" * 80)
        assert len(prop.decided_by) == 60

    asyncio.run(go())
    assert DecideRequest.model_json_schema()["properties"]["by"]["maxLength"] == 30


def test_to_dict_has_exactly_the_nine_baseline_keys():
    async def go():
        hub, inc = await hub_with_incident()
        prop = await hub.create_proposal(inc.id, "fix")
        assert set(prop.to_dict()) == BASELINE
        assert "decision_origin" in prop.to_full_dict()

    asyncio.run(go())


def test_incident_opening_and_service_id_are_recorded_at_creation():
    async def go():
        hub, inc = await hub_with_incident("svc7")
        prop = await hub.create_proposal(inc.id, "fix")
        assert (prop.service_id, prop.incident_opening) == ("svc7", 1)
        hub2, inc2 = await hub_with_incident(None)
        assert (await hub2.create_proposal(inc2.id, "fix")).service_id is None

    asyncio.run(go())
