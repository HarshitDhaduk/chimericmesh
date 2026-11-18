"""Proposal ids are unique per hub run, so an id from an earlier run never matches a new proposal."""
import asyncio

import httpx

from chimericmesh.api import create_app
from chimericmesh.commands import Command, handle_command
from chimericmesh.hub import RUN_TAG_ALPHABET, RUN_TAG_LEN, Hub
from chimericmesh.parsers import parse_syslog

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


async def _hub_with_incident(tag):
    hub = Hub(run_tag=tag)
    for i in range(3):
        await hub.ingest(parse_syslog(DISK % i))
    return hub, hub.incidents()[0].id


def test_default_tag_uses_alphabet_and_length():
    tag = Hub().run_tag
    assert len(tag) == RUN_TAG_LEN and set(tag) <= set(RUN_TAG_ALPHABET)


def test_ids_are_sequential_within_a_run_and_differ_between_runs():
    async def go():
        a, ia = await _hub_with_incident("aaa")
        b, ib = await _hub_with_incident("bbb")
        a1, a2 = await a.create_proposal(ia, "x"), await a.create_proposal(ia, "y")
        b1 = await b.create_proposal(ib, "x")
        assert (a1.id, a2.id, b1.id) == ("p1-aaa", "p2-aaa", "p1-bbb")
        assert a.get_proposal(b1.id) is None and b.get_proposal(a1.id) is None
    asyncio.run(go())


def test_stats_report_run_id_and_started_at():
    hub = Hub(run_tag="k7f", clock=lambda: 1234.5)
    s = hub.stats()
    assert s["run_id"] == "k7f" and s["started_at"] == 1234.5 and s["uptime_seconds"] == 0


def test_old_id_after_restart_is_404_over_rest_and_no_proposal_over_irc():
    async def go():
        old, iid = await _hub_with_incident("old")
        approved = await old.create_proposal(iid, "edit upstream.py")
        await old.decide(approved.id, True, "alice")
        # the hub restarts: a new run, and a new unrelated proposal takes sequence number 1
        new, niid = await _hub_with_incident("new")
        fresh = await new.create_proposal(niid, "Check the target service is listening and healthy")
        assert fresh.id == "p1-new" and fresh.id != approved.id
        app = create_app(new, start_listeners=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get(f"/api/proposals/{approved.id}")).status_code == 404
            assert (await c.get("/api/proposals/p1")).status_code == 404
            assert (await c.post(f"/api/proposals/{approved.id}/complete", json={"note": "x"})).status_code == 404
            assert (await c.get(f"/api/proposals/{fresh.id}")).json()["status"] == "pending"
        out = await handle_command(new, "alice", "#ops", Command("approve", (approved.id,)))
        assert "no proposal" in out[0] and fresh.status == "pending"
        out = await handle_command(new, "alice", "#ops", Command("approve", ("P1",)))
        assert "no proposal" in out[0] and fresh.status == "pending"
        out = await handle_command(new, "alice", "#ops", Command("approve", ("P1-NEW",)))
        assert out[0].endswith("p1-new approved")
    asyncio.run(go())