"""The approvals router (task 6.1): token rules, status codes, bodies, audit rows."""
from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest
from approvals_helpers import make_manager, open_incident
from fastapi import FastAPI
from test_approvals_create import GOOD, WORDS
from test_approvals_reduce import fields

from chimericmesh.approvals.errors import RuleConflict
from chimericmesh.approvals.models import RuleFields
from chimericmesh.approvals.routes import build_router, register_error_handlers
from chimericmesh.services.access import AccessControl
from chimericmesh.services.deps import make_require_admin
from chimericmesh.services.limiter import ReduceAuditLimiter
from chimericmesh.services.token import TokenStore


class Env:
    def __init__(self, *, enforce: bool = True, mode: str = "store", limiter=None):
        self.rig = make_manager(mode, limiter=limiter)
        self.tokens = TokenStore.open(None, in_memory=True, redact=self.rig.store.add_redaction)
        self.token = self.tokens.current()
        self.access = (AccessControl(enforce=True, tokens=self.tokens, allowed_hosts=frozenset({"testserver"}),
                                     audit=self.rig.store.audit) if enforce else AccessControl.disabled())
        self.app = FastAPI()
        self.app.include_router(build_router(self.rig.manager, self.access, make_require_admin(self.access)))
        register_error_handlers(self.app)
        asyncio.run(self.rig.add_service())

    def call(self, method, url, *, token: bool | str = True, ip="7.7.7.7", **kw):
        headers = dict(kw.pop("headers", {}))
        if token is True:
            headers["X-Admin-Token"] = self.token
        elif token:
            headers["X-Admin-Token"] = token

        async def run():
            transport = httpx.ASGITransport(app=self.app, client=(ip, 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(method, url, headers=headers, **kw)

        return asyncio.run(run())

    def audit(self, **kw):
        return asyncio.run(self.rig.store.audit.read(500, **kw))

    def rules_in_db(self):
        return asyncio.run(self.rig.store.read(lambda tx: [r[0] for r in tx.all("SELECT id FROM approval_rules")]))

    def close(self):
        self.rig.close()


@pytest.fixture()
def env():
    e = Env()
    yield e
    e.close()


NEW = {"service_id": "api-gw", "match_type": "signature", "match_value": "disk full on /var"}


def make_rule(env, **kw):
    r = env.call("POST", "/api/rules", json={**NEW, **kw})
    assert r.status_code == 201, r.text
    return r.json()


WIDEN = [
    ("POST", "/api/rules", NEW),
    ("PATCH", "/api/rules/r1", {"max_lines": 5}),
    ("POST", "/api/rules/r1/enable", None),
    ("POST", "/api/rules/resume", None),
    ("POST", "/api/proposals/p1/rule", None),
]


@pytest.mark.parametrize("method,url,body", WIDEN)
def test_widening_routes_need_the_token_and_change_nothing_without_it(env, method, url, body):
    make_rule(env)
    env.call("POST", "/api/rules/r1/pause")
    before = env.rules_in_db(), env.rig.manager.rule("r1")
    for token in (False, "wrong-token"):
        r = env.call(method, url, token=token, json=body) if body is not None else env.call(method, url, token=token)
        assert r.status_code == 401, (method, url, token)
    assert (env.rules_in_db(), env.rig.manager.rule("r1")) == before
    assert env.rig.manager.paused is False


def test_a_401_writes_one_rule_auth_failed_row_with_the_rule_id_and_no_token(env):
    make_rule(env)
    env.call("POST", "/api/rules/r1/pause")
    r = env.call("POST", "/api/rules/r1/enable", token="SECRET-WRONG-TOKEN")
    assert r.status_code == 401
    rows = [x for x in env.audit() if x["action"] == "rule_auth_failed"]
    assert len(rows) == 1 and rows[0]["rule_id"] == "r1" and rows[0]["actor"].startswith("client:7.7.7.7")
    assert "SECRET-WRONG-TOKEN" not in str(rows) and env.token not in str(rows)


def test_a_flood_of_wrong_tokens_is_bounded_and_429_writes_no_row(env):
    codes = [env.call("POST", "/api/rules", token="wrong", json=NEW).status_code for _ in range(100)]
    assert 429 in codes and codes.count(401) <= 10
    rows = [x for x in env.audit() if x["action"] == "rule_auth_failed"]
    assert 1 <= len(rows) <= 10
    blocked = env.call("POST", "/api/rules", token="wrong", json=NEW)
    assert blocked.status_code == 429 and "retry-after" in blocked.headers
    assert len([x for x in env.audit() if x["action"] == "rule_auth_failed"]) == len(rows)


def test_create_rule_success_and_bodies(env):
    rule = make_rule(env, max_lines=50, note="n")
    assert rule["id"] == "r1" and rule["status"] == "active" and rule["max_lines"] == 50
    assert rule["uses_last_hour"] == 0 and rule["remaining_this_hour"] == 3 and rule["created_by"].startswith("rest:")
    for extra in ({"created_by": "me"}, {"status": "revoked"}, {"id": "r9"}, {"use_count": 4},
                  {"code_folder": "/"}, {"surprise": 1}):
        assert env.call("POST", "/api/rules", json={**NEW, **extra}).status_code == 422, extra
    bad = env.call("POST", "/api/rules", json={**NEW, "max_per_hour": 11})
    assert bad.status_code == 422 and bad.json()["field"] == "max_per_hour" and "detail" in bad.json()
    assert env.call("POST", "/api/rules", json={**NEW, "service_id": "nope"}).status_code == 422


def test_rule_count_limit_is_409(env):
    for n in range(20):
        make_rule(env, match_value=f"s{n}")
    assert env.call("POST", "/api/rules", json={**NEW, "match_value": "one more"}).status_code == 409


def test_patch_enable_and_resume(env):
    make_rule(env)
    p = env.call("PATCH", "/api/rules/r1", json={"max_lines": 7})
    assert p.status_code == 200 and p.json()["max_lines"] == 7
    assert env.call("PATCH", "/api/rules/r1", json={"match_value": "x"}).status_code == 422
    assert env.call("PATCH", "/api/rules/r9", json={"max_lines": 1}).status_code == 404
    assert env.call("POST", "/api/rules/r1/enable").status_code == 409          # active, not suspended
    env.call("POST", "/api/rules/r1/pause", token=False)
    assert env.call("POST", "/api/rules/r1/enable").status_code == 200
    assert env.call("POST", "/api/rules/r9/enable").status_code == 404
    env.call("POST", "/api/rules/pause", token=False)
    assert env.call("POST", "/api/rules/resume").json()["paused"] is False
    assert env.call("POST", "/api/rules/r1/revoke", token=False).status_code == 200
    assert env.call("PATCH", "/api/rules/r1", json={"max_lines": 1}).status_code == 409


def test_resume_conflict_is_409(env, monkeypatch):
    async def conflict(*a, **k):
        raise RuleConflict("auto-approval was paused again while resuming")

    monkeypatch.setattr(env.rig.manager, "resume", conflict)
    r = env.call("POST", "/api/rules/resume")
    assert r.status_code == 409 and "paused again" in r.json()["detail"]


def test_reduce_routes_need_no_token_and_are_never_refused(env):
    make_rule(env)
    make_rule(env, match_value="two")
    assert env.call("POST", "/api/rules/r1/pause", token=False).json()["status"] == "suspended"
    assert env.call("POST", "/api/rules/r1/pause", token=False).status_code == 200      # already suspended
    assert env.call("POST", "/api/rules/r2/revoke", token=False).json()["status"] == "revoked"
    again = env.call("POST", "/api/rules/r2/revoke", token=False)
    assert again.status_code == 200 and again.json()["status"] == "revoked"
    assert env.call("POST", "/api/rules/r2/pause", token=False).json()["status"] == "revoked"
    assert env.call("POST", "/api/rules/r9/revoke", token=False).status_code == 404
    assert env.call("POST", "/api/rules/r9/pause", token=False).status_code == 404
    assert env.call("POST", "/api/rules/pause", token=False).json()["paused"] is True


def test_withdraw_of_a_pending_and_a_done_proposal(env):
    async def prepare():
        iid = await open_incident(env.rig.hub)
        pending = await env.rig.hub.create_proposal(iid, "fix")
        other = await env.rig.hub.create_proposal(await open_incident(env.rig.hub, message="something else broke"),
                                                  "fix 2")
        await env.rig.hub.decide(other.id, True, "alice")
        await env.rig.hub.complete(other.id, "ok")
        return pending.id, other.id

    pending, done = asyncio.run(prepare())
    r = env.call("POST", f"/api/proposals/{pending}/withdraw", token=False)
    assert r.status_code == 200 and r.json()["status"] == "denied" and r.json()["decided_by"].startswith("withdrawn:")
    r = env.call("POST", f"/api/proposals/{done}/withdraw", token=False)
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert env.call("POST", "/api/proposals/p99/withdraw", token=False).status_code == 404


def test_reads_need_no_token_and_show_usage(env):
    make_rule(env)
    listing = env.call("GET", "/api/rules", token=False).json()
    assert [r["id"] for r in listing] == ["r1"] and "uses_last_hour" in listing[0]
    assert env.call("GET", "/api/rules/r1", token=False).json()["remaining_this_hour"] == 3
    assert env.call("GET", "/api/rules/r9", token=False).status_code == 404
    assert env.call("GET", "/api/rules?status=revoked", token=False).json() == []
    assert env.call("GET", "/api/rules?status=weird", token=False).status_code == 422
    st = env.call("GET", "/api/approvals/status", token=False).json()
    assert st["paused"] is False and st["bounds"]["max_per_hour"] == {"min": 1, "max": 10, "default": 3}
    assert st["cooldown_seconds"] == 86400


def test_rule_defaults_and_rule_from_a_proposal(env):
    async def prepare():
        iid = await open_incident(env.rig.hub)
        pending = await env.rig.hub.create_proposal(iid, "fix")
        iid2 = await open_incident(env.rig.hub, message="another distinct failure message")
        good = await env.rig.hub.create_proposal(iid2, "fix 2")
        await env.rig.hub.decide(good.id, True, "alice")
        iid3 = await open_incident(env.rig.hub, host="stranger", message="third distinct failure message")
        orphan = await env.rig.hub.create_proposal(iid3, "fix 3")
        await env.rig.hub.decide(orphan.id, True, "alice")
        return pending.id, good.id, orphan.id

    pending, good, orphan = asyncio.run(prepare())
    d = env.call("GET", f"/api/proposals/{good}/rule-defaults", token=False)
    assert d.status_code == 200 and d.json()["service_id"] == "api-gw" and d.json()["max_files"] == 5
    assert env.call("GET", f"/api/proposals/{pending}/rule-defaults", token=False).status_code == 409
    assert env.call("GET", f"/api/proposals/{orphan}/rule-defaults", token=False).status_code == 422
    assert env.call("GET", "/api/proposals/p99/rule-defaults", token=False).status_code == 404
    assert env.rules_in_db() == []                                                  # reading creates nothing
    made = env.call("POST", f"/api/proposals/{good}/rule", json={"max_lines": 40})
    assert made.status_code == 201 and made.json()["source_proposal_id"] == good and made.json()["max_lines"] == 40
    assert env.call("POST", f"/api/proposals/{pending}/rule").status_code == 409
    assert env.call("POST", f"/api/proposals/{good}/rule", json={"service_id": "x"}).status_code == 422


def test_audit_filters_limit_and_before(env):
    make_rule(env)
    make_rule(env, match_value="two")
    env.call("POST", "/api/rules/r1/revoke", token=False)
    rows = env.call("GET", "/api/audit", token=False).json()
    assert rows[0]["id"] > rows[-1]["id"]                                           # newest first
    assert {r["rule_id"] for r in env.call("GET", "/api/audit?rule_id=r2", token=False).json()} == {"r2"}
    assert env.call("GET", "/api/audit?service_id=api-gw&limit=1", token=False).json().__len__() == 1
    assert env.call("GET", "/api/audit?limit=0", token=False).status_code == 422
    assert env.call("GET", "/api/audit?limit=501", token=False).status_code == 422
    older = env.call("GET", f"/api/audit?before={rows[0]['id']}", token=False).json()
    assert all(r["id"] < rows[0]["id"] for r in older)


def test_enforcement_off_leaves_widening_routes_open():
    env = Env(enforce=False)
    try:
        assert env.call("POST", "/api/rules", token=False, json=NEW).status_code == 201
        assert env.call("POST", "/api/rules/resume", token=False).status_code == 200
    finally:
        env.close()


def test_every_route_handler_is_async():
    router = build_router(make_manager("bare").manager, AccessControl.disabled(), lambda r: None)
    for route in router.routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path


def test_eleven_pauses_in_a_minute_all_take_effect_with_a_coalesced_row():
    env = Env(limiter=None)
    env.rig.manager._limiter = ReduceAuditLimiter(env.rig.store.audit, now=env.rig.clock)
    try:
        for n in range(11):
            make_rule(env, match_value=f"s{n}")
        for n in range(11):
            assert env.call("POST", f"/api/rules/r{n + 1}/pause", token=False).json()["status"] == "suspended"
        asyncio.run(env.rig.manager._limiter.aclose())
        actions = [r["action"] for r in env.audit()]
        assert actions.count("rule_paused") == 10 and actions.count("reduce_ops_suppressed") == 1
    finally:
        env.close()
