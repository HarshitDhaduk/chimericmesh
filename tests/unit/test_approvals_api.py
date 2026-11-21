"""The changed proposal routes in api.py (task 6.2), through ``create_app``, in both modes."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import os
import tempfile

import httpx
import pytest
from approvals_helpers import FakeClock, open_incident

from chimericmesh.api import build_registry, create_app
from chimericmesh.hub import Hub
from chimericmesh.store import Store

GOOD = {"kind": "code_change", "files": ["upstream.py"], "estimated_lines": 10, "verification": "run tests"}


class Rig:
    def __init__(self, mode: str, monkeypatch):
        self.mode = mode
        self.clock = FakeClock()
        self.tmp = tempfile.TemporaryDirectory()
        monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(self.tmp.name, "home"))
        self.folder = os.path.realpath(os.path.join(self.tmp.name, "code"))
        os.makedirs(self.folder)
        if mode == "store":
            os.makedirs(os.path.join(self.tmp.name, "db"))
            self.store = Store.open(os.path.join(self.tmp.name, "db", "hub.db"), registry=build_registry())
            self.hub = Hub(store=self.store, clock=self.clock)
        else:
            self.store = None
            self.hub = Hub(clock=self.clock)
        self.app = create_app(self.hub, start_listeners=False, enforce_access=False)
        self.manager = self.app.state.approvals
        assert self.call("POST", "/api/services", json={"id": "api-gw", "name": "gw", "hosts": ["web01"],
                                                       "code_folder": self.folder}).status_code == 201

    def call(self, method, url, **kw):
        async def run():
            transport = httpx.ASGITransport(app=self.app, client=("7.7.7.7", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(method, url, **kw)

        return asyncio.run(run())

    def incident(self, message="disk full on /var"):
        return asyncio.run(open_incident(self.hub, message=message))

    def rule(self, **kw):
        r = self.call("POST", "/api/rules", json={"service_id": "api-gw", "match_type": "signature",
                                                  "match_value": "disk full on /var", **kw})
        assert r.status_code == 201, r.text
        return r.json()

    def close(self):
        if self.store is not None:
            self.store.close()
        self.tmp.cleanup()


@pytest.fixture(params=["bare", "store"])
def rig(request, monkeypatch):
    r = Rig(request.param, monkeypatch)
    yield r
    r.close()


def test_the_old_request_shape_still_works_and_the_response_has_the_new_keys(rig):
    iid = rig.incident()
    r = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", "details": "d"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["approval"] is None and body["files"] is None
    assert body["kind"] is None and "decision_origin" in body


def test_a_matching_rule_approves_in_the_create_response(rig):
    iid = rig.incident()
    rig.rule()
    r = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **GOOD})
    body = r.json()
    assert r.status_code == 201 and body["status"] == "approved" and body["decided_by"] == "rule:r1"
    assert body["approved_by_rule"] == "r1" and body["decision_origin"] == "hub"
    assert body["approval"]["by"] == "rule" and body["approval"]["rule_id"] == "r1"
    assert body["approval"]["rule_status_now"] == "active" and body["approval"]["expired"] is False


def test_invalid_fields_give_422_with_the_field_name(rig):
    iid = rig.incident()
    for patch, field in (({"files": ["../x.py"]}, "files"), ({"files": ["a.py"] * 21}, "files"),
                         ({"estimated_lines": 5000}, "estimated_lines"), ({"estimated_lines": True}, "estimated_lines"),
                         ({"verification": "v" * 301}, "verification"), ({"kind": "config"}, "kind")):
        r = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **patch})
        assert r.status_code == 422 and r.json()["field"] == field, patch


def test_unknown_incident_is_404(rig):
    assert rig.call("POST", "/api/proposals", json={"incident_id": "nope", "action": "x"}).status_code == 404


def test_forbidden_keys_in_the_body_are_ignored_not_rejected(rig):
    iid = rig.incident()
    r = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", "status": "approved",
                                                 "decided_by": "root", "approved_by_rule": "r1", "surprise": 1})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert r.json()["decided_by"] == "" and r.json()["approved_by_rule"] is None


def test_limit_errors_are_409_and_429(monkeypatch):
    # only create_app() without a hub or a store (the production path) enforces the limits
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(tmp.name, "home"))
    os.makedirs(os.path.join(tmp.name, "db"))
    monkeypatch.setenv("CHIMERICMESH_DB", os.path.join(tmp.name, "db", "h.db"))
    app = create_app(start_listeners=False, enforce_access=False)
    store = app.state.store
    hub = app.state.hub
    assert hub.enforce_limits is True

    def call(method, url, **kw):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(run())

    try:
        iid = asyncio.run(open_incident(hub, host="stranger"))
        first = call("POST", "/api/proposals", json={"incident_id": iid, "action": "one"})
        assert first.status_code == 201
        again = call("POST", "/api/proposals", json={"incident_id": iid, "action": "two"})
        assert again.status_code == 409 and again.json()["proposal_id"] == first.json()["id"]
        words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda"]
        codes = []
        for w in words:
            other = asyncio.run(open_incident(hub, host="stranger", message=f"failure kind {w} happened"))
            codes.append(call("POST", "/api/proposals", json={"incident_id": other, "action": "x"}))
        assert codes[-1].status_code == 429 and int(codes[-1].headers["retry-after"]) >= 1
    finally:
        store.close()
        tmp.cleanup()


def test_reads_carry_the_approval_object_and_follow_the_column(rig):
    iid = rig.incident()
    rig.rule()
    auto = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **GOOD}).json()
    iid2 = rig.incident("another distinct failure to approve by hand")
    manual = rig.call("POST", "/api/proposals", json={"incident_id": iid2, "action": "fix2"}).json()
    rig.call("POST", f"/api/proposals/{manual['id']}/decide", json={"approve": True, "by": "ruleman"})
    got = rig.call("GET", f"/api/proposals/{manual['id']}").json()
    assert got["approval"]["by"] == "user" and got["decided_by"] == "ruleman" and got["approved_by_rule"] is None
    listing = {p["id"]: p for p in rig.call("GET", "/api/proposals").json()}
    assert listing[auto["id"]]["approval"]["by"] == "rule"
    assert rig.call("GET", "/api/proposals/p99").status_code == 404


def test_reserved_deciders_are_refused_and_nothing_changes(rig):
    iid = rig.incident()
    prop = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix"}).json()
    for by in ("rule:r1", "RULE-REVOKED:r1", "withdrawn:x", "expired:y", "expired-z", "  rule:r1"):
        r = rig.call("POST", f"/api/proposals/{prop['id']}/decide", json={"approve": True, "by": by})
        assert r.status_code == 422, by
    assert rig.call("GET", f"/api/proposals/{prop['id']}").json()["status"] == "pending"
    assert rig.call("POST", f"/api/proposals/{prop['id']}/decide", json={"approve": True, "by": "x" * 31}).status_code == 422
    ok = rig.call("POST", f"/api/proposals/{prop['id']}/decide", json={"approve": True, "by": "expired1"})
    assert ok.status_code == 200 and ok.json()["approved_by_rule"] is None


def test_complete_accepts_the_completion_fields_and_409s_an_expired_approval(rig):
    iid = rig.incident()
    rig.rule()
    prop = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **GOOD}).json()
    done = rig.call("POST", f"/api/proposals/{prop['id']}/complete",
                    json={"note": "n", "files_changed": ["upstream.py"], "lines_changed": 3,
                          "verification_result": "passed"})
    assert done.status_code == 200 and done.json()["status"] == "done" and done.json()["lines_changed"] == 3
    bad = rig.call("POST", f"/api/proposals/{prop['id']}/complete", json={"verification_result": "maybe"})
    assert bad.status_code in (409, 422)
    iid2 = rig.incident("another distinct failure for the expiry case")
    rig.call("POST", "/api/rules", json={"service_id": "api-gw", "match_type": "category", "match_value": "unknown"})
    second = rig.call("POST", "/api/proposals", json={"incident_id": iid2, "action": "fix", **GOOD}).json()
    assert second["status"] == "approved"
    rig.clock.advance(1801)
    late = rig.call("POST", f"/api/proposals/{second['id']}/complete", json={"note": "late"})
    assert late.status_code == 409


def test_expire_due_runs_before_reads_and_a_dead_store_does_not_break_them(rig, monkeypatch):
    calls = []
    real = rig.manager.expire_due

    async def spy():
        calls.append(1)
        await real()

    monkeypatch.setattr(rig.manager, "expire_due", spy)
    iid = rig.incident()
    rig.call("GET", "/api/rules")
    rig.call("GET", "/api/proposals")
    prop = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix"}).json()
    rig.call("GET", f"/api/proposals/{prop['id']}")
    rig.call("GET", f"/api/proposals/{prop['id']}/rule-defaults")
    rig.call("POST", f"/api/proposals/{prop['id']}/complete", json={})
    assert len(calls) >= 5


def test_reads_stay_200_while_the_sweep_holds_the_lock_and_show_expired(rig):
    iid = rig.incident()
    rig.rule()
    prop = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **GOOD}).json()
    rig.clock.advance(1801)

    async def read_while_sweeping():
        async with rig.manager._sweep_lock:                  # expire_due returns at once and does nothing
            transport = httpx.ASGITransport(app=rig.app, client=("7.7.7.7", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                one = await c.get(f"/api/proposals/{prop['id']}")
                many = await c.get("/api/proposals")
                return one, many

    one, many = asyncio.run(read_while_sweeping())
    assert one.status_code == 200 and many.status_code == 200
    assert one.json()["status"] == "approved" and one.json()["approval"]["expired"] is True


def test_reads_stay_200_with_a_failing_store(rig):
    iid = rig.incident()
    rig.rule()
    prop = rig.call("POST", "/api/proposals", json={"incident_id": iid, "action": "fix", **GOOD}).json()
    rig.clock.advance(1801)
    from chimericmesh.store import StoreError

    async def broken(fn):
        raise StoreError("down")

    target = rig.manager._store
    real = target.transact
    target.transact = broken
    try:
        got = rig.call("GET", f"/api/proposals/{prop['id']}")
        listing = rig.call("GET", "/api/rules")
    finally:
        target.transact = real
    assert got.status_code == 200 and listing.status_code == 200
    if rig.mode == "store":                       # the write could not happen: the mirror still says approved
        assert got.json()["status"] == "approved" and got.json()["approval"]["expired"] is True
    else:                                         # a bare hub denies in memory; its store is not on that path
        assert got.json()["status"] in ("approved", "denied")


def test_service_filter_is_applied_before_the_limit(rig):
    rig.hub.enforce_limits = False
    asyncio.run(rig.hub.ingest(__import__("chimericmesh.events", fromlist=["Event"]).Event(
        source="syslog", host="other", app="x", severity=3, message="other host failure kind")))
    a = rig.incident()
    ids = []
    for n in range(3):
        ids.append(rig.call("POST", "/api/proposals", json={"incident_id": a, "action": f"a{n}"}).json()["id"])
    stranger = asyncio.run(open_incident(rig.hub, host="stranger", message="stranger failure here"))
    for n in range(205 if rig.mode == "store" else 150):
        rig.hub.enforce_limits = False
        asyncio.run(rig.hub.create_proposal(stranger, f"b{n}"))
    got = rig.call("GET", "/api/proposals?service_id=api-gw&limit=2").json()
    if rig.mode == "store":
        assert [p["id"] for p in got] == ids[-2:]
    assert rig.call("GET", "/api/proposals?service_id=unassigned&limit=1").status_code == 200
