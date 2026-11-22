"""Success Criteria 1 to 9 of auto-approval through the app (task 10.1), for a plain ``Hub()`` and a store-backed hub.

REST through ``httpx.ASGITransport``, a fake clock through ``Hub(clock=...)``, reopen window 300 s.
"""
from __future__ import annotations

from approvals_helpers import FakeClock, collect_garbage  # noqa: F401

import asyncio
import os
import tempfile

import httpx
import pytest

from chimericmesh.api import build_registry, create_app
from chimericmesh.hub import Hub
from chimericmesh.store import Store

BUG = "<27>Nov 21 10:00:00 {host} app[1]: ERROR: upstream timed out connecting to 10.0.0.9:5432"
FIX = {"kind": "code_change", "files": ["upstream.py"], "estimated_lines": 20, "verification": "python -m pytest"}
DONE_OK = {"note": "fixed", "files_changed": ["upstream.py"], "lines_changed": 12, "verification_result": "passed"}
H = 3600.0


class App:
    def __init__(self, mode: str, monkeypatch):
        self.mode = mode
        self.tmp = tempfile.TemporaryDirectory()
        monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(self.tmp.name, "home"))
        monkeypatch.setenv("CHIMERICMESH_REOPEN_AFTER_SECONDS", "300")
        self.folder = os.path.realpath(os.path.join(self.tmp.name, "code"))
        os.makedirs(self.folder)
        self.clock = FakeClock()
        if mode == "store":
            os.makedirs(os.path.join(self.tmp.name, "db"))
            self.store = Store.open(os.path.join(self.tmp.name, "db", "i.db"), registry=build_registry())
            self.hub = Hub(store=self.store, clock=self.clock)
        else:
            self.store = None
            self.hub = Hub(clock=self.clock)
        self.app = create_app(self.hub, start_listeners=False, enforce_access=True)
        self.token = self.app.state.access.tokens.current()
        self.manager = self.app.state.approvals
        assert self.post("/api/services", {"id": "api-gw", "name": "gateway", "hosts": ["web01", "web02"],
                                           "code_folder": self.folder}).status_code == 201

    def req(self, method, url, body=None, *, token=True):
        headers = {"X-Admin-Token": self.token} if token else {}

        async def go():
            transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 5))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.request(method, url, json=body, headers=headers)

        return asyncio.run(go())

    def post(self, url, body=None, **kw):
        return self.req("POST", url, body, **kw)

    def get(self, url):
        return self.req("GET", url, token=False)

    def bug(self, host="web01", n=3):
        for _ in range(n):
            assert self.post("/api/ingest", {"source": "syslog", "line": BUG.format(host=host)},
                             token=False).status_code == 202
        return next(i["id"] for i in self.get("/api/incidents").json() if i["host"] == host)

    def propose(self, iid, **extra):
        r = self.post("/api/proposals", {"incident_id": iid, "action": "add retry with backoff", **{**FIX, **extra}},
                      token=False)
        assert r.status_code == 201, r.text
        return r.json()

    def audit(self, **q):
        query = "&".join(f"{k}={v}" for k, v in q.items())
        return self.get("/api/audit?limit=500" + (f"&{query}" if query else "")).json()

    def lines(self):
        return [m.text for m in self.hub.transcript if m.kind == "bot"]

    def reopen(self, iid, host="web01", after=301):
        assert self.post(f"/api/incidents/{iid}/resolve", {"note": "done"}, token=False).status_code == 200
        self.clock.advance(after)
        self.bug(host, n=1)
        return self.get(f"/api/incidents/{iid}").json()["opening"]

    def close(self):
        if self.store is not None:
            self.store.close()
        self.tmp.cleanup()


@pytest.fixture(params=["bare", "store"])
def app(request, monkeypatch):
    a = App(request.param, monkeypatch)
    yield a
    a.close()


def first_rule(app):
    """Success Criterion 1, first half: a manual fix that passed becomes rule r1."""
    iid = app.bug()
    p1 = app.propose(iid)
    assert p1["status"] == "pending" and p1["approval"] is None
    assert app.post(f"/api/proposals/{p1['id']}/decide", {"approve": True, "by": "alice"}).status_code == 200
    done = app.post(f"/api/proposals/{p1['id']}/complete", DONE_OK, token=False)
    assert done.status_code == 200 and done.json()["status"] == "done"
    r = app.post(f"/api/proposals/{p1['id']}/rule", {})
    assert r.status_code == 201 and r.json()["id"] == "r1" and r.json()["source_proposal_id"] == p1["id"]
    return iid, p1


def test_sc1_a_reopened_bug_is_auto_approved_by_the_rule_with_audit_and_one_line(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)                    # outside the cooldown, so the rule may act
    assert app.reopen(iid) == 2
    before = len(app.lines())
    p2 = app.propose(iid)
    assert p2["status"] == "approved" and p2["decided_by"] == "rule:r1" and p2["approved_by_rule"] == "r1"
    assert p2["approval"]["by"] == "rule" and p2["decision_origin"] == "hub"
    new = [t for t in app.lines()[before:] if p2["id"] in t]
    assert len(new) == 1 and new[0].startswith(f"AUTO-APPROVED {p2['id']} by rule r1 (service api-gw)")
    assert "reply !approve" not in new[0]
    rows = [r for r in app.audit(proposal_id=p2["id"]) if r["action"] == "proposal_auto_approved"]
    assert len(rows) == 1 and rows[0]["actor"] == "rule:r1"
    # the same signature from a second host of the same service also matches
    other = app.bug("web02")
    p3 = app.propose(other)
    assert p3["status"] == "approved" and p3["approved_by_rule"] == "r1"


def test_sc2_out_of_scope_or_too_large_stays_pending_with_the_condition(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)
    app.reopen(iid)
    protected = app.propose(iid, files=[".git/hooks/pre-commit.py"])
    assert protected["status"] == "pending"
    assert any(r["detail"] == "condition=files" for r in app.audit(proposal_id=protected["id"]))
    assert app.post(f"/api/proposals/{protected['id']}/withdraw", token=False).status_code == 200
    big = app.propose(iid, estimated_lines=101)
    assert big["status"] == "pending"
    assert any(r["detail"] == "condition=lines" for r in app.audit(proposal_id=big["id"]))
    outside = app.post("/api/proposals", {"incident_id": iid, "action": "x", **FIX, "files": ["../other/x.py"]},
                       token=False)
    assert outside.status_code == 422 and outside.json()["field"] == "files"         # blockers B1


def test_sc3_the_hourly_limit_stops_the_fourth(app):
    first_rule(app)
    hosts = ["web01", "web02"]
    app.post("/api/rules/r1/revoke", token=False)
    assert app.post("/api/rules", {"service_id": "api-gw", "match_type": "category", "match_value": "network",
                                   "max_per_hour": 3, "max_per_incident": 3}).status_code == 201
    statuses = []
    for n in range(4):
        iid = app.bug(hosts[n % 2]) if n < 2 else None
        if iid is None:
            msg = f"<27>Nov 21 10:00:00 web0{n % 2 + 1} app[1]: ERROR: connection refused to cache node {n}"
            for _ in range(3):
                app.post("/api/ingest", {"source": "syslog", "line": msg}, token=False)
            iid = next(i["id"] for i in app.get("/api/incidents").json() if f"node {n}" in i["sample"])
        statuses.append(app.propose(iid)["status"])
    assert statuses == ["approved", "approved", "approved", "pending"]


def test_sc4_revoke_denies_and_a_folder_change_suspends(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)
    app.reopen(iid)
    p2 = app.propose(iid)
    assert p2["status"] == "approved"
    assert app.post("/api/rules/r1/revoke", token=False).json()["status"] == "revoked"
    got = app.get(f"/api/proposals/{p2['id']}").json()
    assert got["status"] == "denied" and got["decided_by"] == "rule-revoked:r1"
    other = app.bug("web02")
    assert app.propose(other)["status"] == "pending"
    # a new rule, then a code folder change suspends it the same way
    assert app.post("/api/rules", {"service_id": "api-gw", "match_type": "category",
                                   "match_value": "network"}).status_code == 201
    new_folder = os.path.join(os.path.dirname(app.folder), "code2")
    os.makedirs(new_folder)
    assert app.req("PATCH", "/api/services/api-gw", {"code_folder": new_folder}).status_code == 200
    assert app.get("/api/rules/r2").json()["status"] == "suspended"
    assert app.get("/api/rules/r2").json()["status_reason"] == "code_folder_changed"


def test_sc5_no_token_no_widening_and_the_reduce_operations_always_work(app):
    iid, p1 = first_rule(app)
    new = {"service_id": "api-gw", "match_type": "category", "match_value": "network"}
    assert app.post("/api/rules", new, token=False).status_code == 401
    assert app.post("/api/rules/r1/pause", token=False).json()["status"] == "suspended"
    assert app.post("/api/rules/r1/enable", token=False).status_code == 401
    assert app.post("/api/rules/pause", token=False).json()["paused"] is True
    assert app.post("/api/rules/resume", token=False).status_code == 401
    for _ in range(11):
        assert app.post("/api/rules/pause", token=False).status_code == 200
    assert app.post("/api/rules/resume").json()["paused"] is False
    # a done proposal whose fix failed cannot seed a rule
    other = app.bug("web02")
    p = app.propose(other, estimated_lines=999)
    app.post(f"/api/proposals/{p['id']}/decide", {"approve": True, "by": "alice"})
    app.post(f"/api/proposals/{p['id']}/complete", {**DONE_OK, "verification_result": "failed"}, token=False)
    assert app.post(f"/api/proposals/{p['id']}/rule", {}).status_code == 409
    from chimericmesh import mcp_server
    assert not [n for n in dir(mcp_server) if "rule" in n.lower()]


def test_sc6_a_completion_outside_the_folder_suspends_the_rule(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)
    app.reopen(iid)
    p2 = app.propose(iid)
    assert p2["status"] == "approved"
    done = app.post(f"/api/proposals/{p2['id']}/complete", {**DONE_OK, "files_changed": ["../elsewhere/x.py"]},
                    token=False)
    assert done.json()["status"] == "done"
    rule = app.get("/api/rules/r1").json()
    assert rule["status"] == "suspended" and rule["status_reason"] == "completion_out_of_scope"


def test_sc8_the_cooldown_one_line_and_one_row_and_the_late_proposal_still_waits(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)
    app.reopen(iid)
    p2 = app.propose(iid)
    assert p2["status"] == "approved"
    app.post(f"/api/proposals/{p2['id']}/complete", DONE_OK, token=False)
    assert app.reopen(iid, after=H) == 3                          # reopened one hour after the auto-fix
    before = len(app.lines())
    p3 = app.propose(iid)
    assert p3["status"] == "pending" and p3["approved_by_rule"] is None
    new = [t for t in app.lines()[before:] if p3["id"] in t]
    assert len(new) == 1 and p2["id"] in new[0] and f"reply !approve {p3['id']}" in new[0]
    rows = [r for r in app.audit(proposal_id=p3["id"]) if r["action"] == "autofix_cooldown"]
    assert len(rows) == 1 and f"earlier={p2['id']}" in rows[0]["detail"]
    # 30 hours later, for the same opening, it still waits for the user (H1)
    app.post(f"/api/proposals/{p3['id']}/withdraw", token=False)
    app.clock.advance(29 * H)
    assert app.propose(iid)["status"] == "pending"


def test_sc8b_a_reopen_after_the_cooldown_is_auto_approved(app):
    iid, _ = first_rule(app)
    app.clock.advance(25 * H)
    app.reopen(iid)
    p2 = app.propose(iid)
    app.post(f"/api/proposals/{p2['id']}/complete", DONE_OK, token=False)
    app.clock.advance(25 * H)
    app.reopen(iid)
    app.post("/api/rules/r1/pause", token=False)
    app.post("/api/rules/r1/enable")                              # rule r1 stays the same rule
    assert app.propose(iid)["status"] == "approved"


def test_sc9_the_dependency_rows_hold():
    from test_dependency_direction import test_imports_follow_the_table, test_third_party_imports_only_in_wiring

    test_imports_follow_the_table()
    test_third_party_imports_only_in_wiring()


def test_sc5_eleven_pauses_all_take_effect_with_ten_rows_and_the_mcp_list_has_no_rule_tool(app):
    first_rule(app)
    for _ in range(11):                 # each pause follows a resume, so each one changes state
        assert app.post("/api/rules/pause", token=False).json()["paused"] is True
        assert app.post("/api/rules/resume").json()["paused"] is False
    rows = [r for r in app.audit() if r["action"].startswith("rules_paused")]
    assert len(rows) == 10, [r["action"] for r in rows]    # the eleventh is coalesced (test_approvals_reduce)
    from chimericmesh import mcp_server
    names = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    assert names and not [n for n in names if "rule" in n.lower()]