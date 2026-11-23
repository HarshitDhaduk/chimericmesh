"""The agent loop through the real app in both hub modes (agent-loop task 12.1; Success Criteria 3 to 5, 6 and 7).

No Kiro, no sockets: ``httpx.ASGITransport`` over ``create_app``, a fake clock injected through ``Hub(clock=...)``.
``bare`` is a plain in-memory ``Hub()``; ``store`` is a hub over a file store in a temporary directory. Both run
the same assertions.
"""
from __future__ import annotations

from approvals_helpers import FakeClock, collect_garbage, open_incident  # noqa: F401  (autouse: frees apps and stores)

import asyncio
import os
import time

import httpx
import pytest

from chimericmesh.agent.watch import WatchQueue
from chimericmesh.api import build_registry, create_app
from chimericmesh.approvals.models import RuleFields
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
from chimericmesh.store import Store

MODES = ("bare", "store")
MESSAGE = "disk full on /var"


class World:
    def __init__(self, mode: str, tmp_path, *, reopen_after: int | None = None):
        self.clock = FakeClock()
        self.folder = os.path.realpath(str(tmp_path / "code"))
        os.makedirs(self.folder)
        self.source = os.path.join(self.folder, "app.py")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("raise OSError('disk full on /var')\n")
        self.store = None
        os.makedirs(str(tmp_path / "db"), exist_ok=True)
        if mode == "store":
            self.store = Store.open(str(tmp_path / "db" / "hub.db"), registry=build_registry())
            self.hub = Hub(store=self.store, clock=self.clock, reopen_after=reopen_after)
        else:
            self.hub = Hub(clock=self.clock, reopen_after=reopen_after)
        self.app = create_app(self.hub, start_listeners=False)
        self.app.state.agent.watch = WatchQueue(self.app.state.agent._ports, wait_max=1, tick=0.05)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def start(self) -> None:
        await self.app.state.services.create_service(
            ServiceFields(id="api-gw", name="api-gw", hosts=("web01",), code_folder=self.folder), actor="t")

    async def stop(self) -> None:
        await self.client.aclose()
        self.app.state.agent.aclose()
        if self.store is not None:
            self.store.close()

    async def incident(self, host: str = "web01") -> str:
        return await open_incident(self.hub, host=host, message=MESSAGE)

    async def wait(self, **params) -> dict:
        r = await self.client.get("/api/watch/next", params={"timeout_seconds": 1, **params})
        assert r.status_code == 200
        return r.json()

    async def propose(self, iid: str, files: list[str] | None = None) -> dict:
        body = {"incident_id": iid, "action": "bound the retry delay", "details": "d", "kind": "code_change",
                "files": ["app.py"] if files is None else files, "estimated_lines": 5,
                "verification": "python -m pytest -q"}
        r = await self.client.post("/api/proposals", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    async def verify(self, pid: str, files: list[str] | None = None) -> dict:
        r = await self.client.post(f"/api/proposals/{pid}/verify", json={"files": [self.source] if files is None else files})
        assert r.status_code == 200
        return r.json()


def scenario(mode: str, tmp_path, body, **kw):
    async def go():
        world = World(mode, tmp_path, **kw)
        await world.start()
        try:
            return await body(world)
        finally:
            await world.stop()
    return asyncio.run(go())


@pytest.mark.parametrize("mode", MODES)
def test_work_arrives_within_1_5_seconds_of_the_incident_and_times_out_when_quiet(mode, tmp_path):
    async def body(w):
        assert (await w.wait()) == {"status": "timeout", "incidents": [], "approved_proposals": []}
        waiter = asyncio.create_task(w.wait())
        await asyncio.sleep(0.2)
        start = time.monotonic()
        iid = await w.incident()
        result = await asyncio.wait_for(waiter, 1.5)
        assert time.monotonic() - start < 1.5
        assert result["status"] == "work" and [i["id"] for i in result["incidents"]] == [iid]
        assert (await w.wait(exclude_ids=iid))["status"] == "timeout"
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode", MODES)
def test_service_events_digest_and_code_search_answer_for_the_service(mode, tmp_path):
    async def body(w):
        iid = await w.incident()
        events = (await w.client.get("/api/services/api-gw/events")).json()
        assert len(events["events"]) == 3 and all(MESSAGE in e["message"] for e in events["events"])
        digest = (await w.client.get("/api/services/api-gw/digest")).json()
        assert digest["service_id"] == "api-gw" and sum(digest["severity_counts"].values()) >= 3
        hits = (await w.client.get("/api/services/api-gw/code-search", params={"query": "disk full"})).json()
        assert hits["hits"] and hits["hits"][0]["file"] == "app.py"
        assert (await w.client.get(f"/api/incidents/{iid}")).json()["service_id"] == "api-gw"
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode", MODES)
def test_user_approval_lists_the_proposal_and_verify_accepts_the_declared_file(mode, tmp_path):
    async def body(w):
        iid = await w.incident()
        prop = await w.propose(iid)
        assert prop["status"] == "pending" and prop["id"].startswith("p")
        assert (await w.verify(prop["id"]))["ok"] is False                         # not approved yet
        assert (await w.wait(exclude_ids=iid))["approved_proposals"] == []
        assert (await w.hub.decide(prop["id"], True, "alice")).status == "approved"   # what !approve does
        work = await w.wait(exclude_ids=iid)
        assert work["status"] == "work" and [p["id"] for p in work["approved_proposals"]] == [prop["id"]]
        ok = await w.verify(prop["id"])
        assert ok["ok"] is True and ok["status"] == "approved"
        outside = os.path.join(os.path.dirname(w.folder), "elsewhere.py")
        assert (await w.verify(prop["id"], [outside]))["ok"] is False
        undeclared = os.path.join(w.folder, "other.py")
        assert (await w.verify(prop["id"], [undeclared]))["ok"] is False
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode", MODES)
def test_a_rule_approves_at_creation_and_revoking_it_stops_verify_and_the_work_list(mode, tmp_path):
    async def body(w):
        iid = await w.incident()
        rule = await w.app.state.approvals.create_rule(RuleFields("api-gw", "signature", MESSAGE), actor="a")
        prop = await w.propose(iid)
        assert prop["status"] == "approved" and prop["approved_by_rule"] == rule.id
        assert (await w.verify(prop["id"]))["ok"] is True
        assert [p["id"] for p in (await w.wait(exclude_ids=iid))["approved_proposals"]] == [prop["id"]]
        revoked = await w.client.post(f"/api/rules/{rule.id}/revoke")
        assert revoked.status_code == 200
        assert (await w.verify(prop["id"]))["ok"] is False                         # Success Criterion 3, automated
        assert all(p["id"] != prop["id"] for p in (await w.wait(exclude_ids=iid))["approved_proposals"])
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode", MODES)
def test_a_user_denial_hides_the_incident_until_it_reopens(mode, tmp_path):
    async def body(w):
        iid = await w.incident()
        prop = await w.propose(iid)
        assert (await w.hub.decide(prop["id"], False, "alice")).status == "denied"
        assert (await w.wait())["status"] == "timeout"                             # the denied opening stays hidden
        assert (await w.client.post(f"/api/incidents/{iid}/resolve", json={"note": "closed by hand"})).status_code == 200
        w.clock.advance(10)
        await w.incident()                                                         # the same signature opens again
        again = await w.wait()
        assert again["status"] == "work" and [i["id"] for i in again["incidents"]] == [iid]
    scenario(mode, tmp_path, body, reopen_after=5)


@pytest.mark.parametrize("mode", MODES)
def test_complete_then_resolve_closes_the_incident(mode, tmp_path):
    async def body(w):
        iid = await w.incident()
        prop = await w.propose(iid)
        await w.hub.decide(prop["id"], True, "alice")
        done = await w.client.post(f"/api/proposals/{prop['id']}/complete", json={
            "note": "bounded the delay", "files_changed": [w.source], "lines_changed": 4,
            "verification_result": "passed"})
        assert done.status_code == 200 and done.json()["status"] == "done"
        assert (await w.client.post(f"/api/incidents/{iid}/resolve", json={"note": "fixed"})).status_code == 200
        quiet = await w.wait()
        assert quiet["status"] == "timeout" and quiet["approved_proposals"] == []
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode", MODES)
def test_unassigned_events_appear_only_through_the_generic_reads(mode, tmp_path):
    async def body(w):
        stray = await w.incident("stray99")
        assert (await w.client.get(f"/api/incidents/{stray}")).json()["service_id"] in (None, "")
        assert (await w.wait())["status"] == "timeout"                             # not work for any service
        listed = (await w.client.get("/api/incidents")).json()
        assert stray in [i["id"] for i in listed]
        recent = (await w.client.get("/api/events", params={"limit": 10})).json()
        assert any("stray99" in str(e.get("host")) for e in recent)
        scoped = (await w.client.get("/api/services/api-gw/events")).json()
        assert scoped["events"] == []
    scenario(mode, tmp_path, body)


@pytest.mark.parametrize("mode,expected", [("bare", 200), ("store", 205)])
def test_the_bare_hub_keeps_200_proposals_and_the_store_keeps_all(mode, expected, tmp_path):
    """README limit: in memory the proposals list keeps 200; with the store every proposal stays."""
    async def body(w):
        iid = await w.incident()
        for _ in range(205):
            assert await w.hub.create_proposal(iid, "a", "d") is not None
        return len(w.hub.list_proposals(None, limit=500))
    assert scenario(mode, tmp_path, body) == expected

def test_both_modes_expose_the_same_agent_routes(tmp_path):
    async def body(w):
        return {(m, r.path) for r in w.app.routes for m in getattr(r, "methods", ()) or ()
                if r.path.startswith(("/api/watch", "/api/services/{service_id}/", "/api/proposals/{pid}/verify"))}
    a = scenario("bare", tmp_path / "a", body) if (tmp_path / "a").mkdir() is None else None
    b = scenario("store", tmp_path / "b", body) if (tmp_path / "b").mkdir() is None else None
    assert a == b and ("POST", "/api/proposals/{pid}/verify") in a
