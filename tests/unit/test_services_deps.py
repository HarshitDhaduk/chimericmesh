"""The ``require_admin`` dependency (R6.3, R6.4, R6.7)."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from starlette.requests import Request

from chimericmesh.services.access import AccessControl
from chimericmesh.services.deps import make_require_admin
from chimericmesh.services.token import TokenStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Body(BaseModel):
    name: str


def build(store, *, enforce=True):
    tokens = TokenStore.open(None, in_memory=True, redact=store.add_redaction)
    access = AccessControl(enforce=enforce, tokens=tokens, allowed_hosts=frozenset({"testserver"}),
                           audit=store.audit, now=Clock())
    require_admin = make_require_admin(access)
    app = FastAPI()

    @app.get("/api/open")
    async def open_():
        return {"ok": True}

    @app.post("/api/things/{thing_id}", dependencies=[Depends(require_admin)])
    async def thing(thing_id: str, body: Body):
        return {"id": thing_id, "name": body.name}

    @app.delete("/api/things/{thing_id}", dependencies=[Depends(require_admin)])
    async def gone(thing_id: str):
        return {"deleted": thing_id}

    return app, access, require_admin


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("9.9.9.9", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(run())


def audit_rows(store):
    return asyncio.run(store.audit.read(limit=100))


def test_enforcement_off_passes_everything(svc_mem_store):
    app, _, _ = build(svc_mem_store, enforce=False)
    r = call(app, "POST", "/api/things/x1", json={"name": "n"})
    assert r.status_code == 200
    assert audit_rows(svc_mem_store) == []


def test_missing_token_is_401_with_route_template_in_audit(svc_mem_store):
    app, _, _ = build(svc_mem_store)
    r = call(app, "POST", "/api/things/abc123", json={"name": "n"})
    assert r.status_code == 401
    rows = audit_rows(svc_mem_store)
    assert len(rows) == 1
    assert rows[0]["action"] == "auth_failed"
    assert rows[0]["subject"] == "POST /api/things/{thing_id}"
    assert "abc123" not in rows[0]["subject"]
    assert rows[0]["actor"] == "client:9.9.9.9"


def test_wrong_token_is_401_and_right_token_passes(svc_mem_store):
    app, access, _ = build(svc_mem_store)
    assert call(app, "DELETE", "/api/things/a", headers={"X-Admin-Token": "nope"}).status_code == 401
    good = access.tokens.current()
    r = call(app, "DELETE", "/api/things/a", headers={"X-Admin-Token": good})
    assert r.status_code == 200 and r.json() == {"deleted": "a"}
    assert good not in r.text


def test_eleventh_wrong_token_is_429_with_retry_after_and_right_token_still_passes(svc_mem_store):
    app, access, _ = build(svc_mem_store)
    codes = [call(app, "DELETE", "/api/things/a", headers={"X-Admin-Token": "bad"}).status_code
             for _ in range(10)]
    assert codes == [401] * 10
    blocked = call(app, "DELETE", "/api/things/a", headers={"X-Admin-Token": "bad"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    ok = call(app, "DELETE", "/api/things/a", headers={"X-Admin-Token": access.tokens.current()})
    assert ok.status_code == 200
    assert len(audit_rows(svc_mem_store)) == 10


def test_token_in_query_string_is_ignored(svc_mem_store):
    app, access, _ = build(svc_mem_store)
    r = call(app, "DELETE", f"/api/things/a?token={access.tokens.current()}")
    assert r.status_code == 401
    r = call(app, "DELETE", f"/api/things/a?x_admin_token={access.tokens.current()}")
    assert r.status_code == 401


def test_wrong_token_with_schema_invalid_json_gets_401_not_422(svc_mem_store):
    app, access, _ = build(svc_mem_store)
    r = call(app, "POST", "/api/things/a", json={"nope": 1})
    assert r.status_code == 401
    ok_token = {"X-Admin-Token": access.tokens.current()}
    assert call(app, "POST", "/api/things/a", json={"nope": 1}, headers=ok_token).status_code == 422


def test_wrong_token_with_malformed_json_gets_422_because_the_framework_parses_first(svc_mem_store):
    app, _, _ = build(svc_mem_store)
    r = call(app, "POST", "/api/things/a", content=b"{not json",
             headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_require_admin_can_be_awaited_directly(svc_mem_store):
    from fastapi import HTTPException
    _, access, require_admin = build(svc_mem_store)

    def make_request(headers, client=("5.5.5.5", 1)):
        scope = {"type": "http", "method": "POST", "path": "/api/x", "headers": headers,
                 "client": client, "query_string": b""}
        return Request(scope)

    asyncio.run(require_admin(make_request([(b"x-admin-token", access.tokens.current().encode())])))
    try:
        asyncio.run(require_admin(make_request([])))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("expected 401")


def test_missing_client_uses_unknown_address(svc_mem_store):
    from fastapi import HTTPException
    _, _, require_admin = build(svc_mem_store)
    scope = {"type": "http", "method": "GET", "path": "/api/x", "headers": [], "client": None,
             "query_string": b""}
    try:
        asyncio.run(require_admin(Request(scope)))
    except HTTPException as exc:
        assert exc.status_code == 401
    rows = audit_rows(svc_mem_store)
    assert rows[0]["actor"] == "client:unknown"
