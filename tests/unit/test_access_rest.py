"""REST access rules with enforcement on: Host guard, body cap, token on decide and terminal approvals
(R6.3, R6.9, R6.10, R7.1 to R7.3, R7.5, Success Criteria 2, 3, 9)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from chimericmesh.api import create_app
from chimericmesh.hub import Hub
from chimericmesh.listeners import IrcServer
from chimericmesh.parsers import parse_syslog

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"
BASE = "http://127.0.0.1:8000"


def make(enforce=True, operators=None):
    hub = Hub()
    hub.operators = operators
    app = create_app(hub, start_listeners=False, enforce_access=enforce)

    async def quiet(channel, text):
        pass
    hub.irc_broadcast = quiet

    async def seed():
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        inc = hub.incidents()[0]
        prop = await hub.create_proposal(inc.id, "rotate logs")
        return inc, prop
    inc, prop = asyncio.run(seed())
    token = app.state.access.tokens.current() if enforce else ""
    return hub, app, inc, prop, token


def call(app, method, url, base=BASE, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=base) as c:
            return await c.request(method, url, **kw)
    return asyncio.run(run())


@pytest.fixture
def env():
    hub, app, inc, prop, token = make()
    yield hub, app, inc, prop, token
    app.state.store.close()


# ---- Host and Origin ---------------------------------------------------------------------------

def test_host_guard(env):
    hub, app, *_ = env
    assert call(app, "GET", "/api/stats", headers={"Host": "evil.example"}).status_code == 403
    assert call(app, "GET", "/api/stats", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert call(app, "GET", "/api/stats", headers={"Host": "localhost"}).status_code == 200
    assert call(app, "GET", "/", headers={"Host": "evil.example"}).status_code == 200
    assert call(app, "GET", "/api/stats", base="http://testserver").status_code == 403
    off_hub, off_app, *_ = make(enforce=False)
    assert call(off_app, "GET", "/api/stats", base="http://testserver").status_code == 200
    off_app.state.store.close()


def test_origin_check_still_blocks_cross_origin_post(env):
    hub, app, inc, prop, token = env
    r = call(app, "POST", "/api/irc/say", json={"text": "hi"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and "cross-origin" in r.text


def test_guard_order_host_first_then_body_cap(env):
    hub, app, *_ = env
    big = b"x" * (10 * 1024 * 1024)
    r = call(app, "POST", "/api/ingest", content=big,
             headers={"Host": "evil.example", "content-type": "application/json"})
    assert r.status_code == 403
    r = call(app, "POST", "/api/ingest", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_streamed_response_passes_both_guards_unbuffered(env):
    # Unbuffered chunk delivery of each guard is pinned in test_services_asgi.py; here: both are plain
    # ASGI classes (not BaseHTTPMiddleware) and HostGuard is outermost.
    from starlette.middleware.base import BaseHTTPMiddleware
    hub, app, *_ = env
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names.index("HostGuard") < names.index("BodyLimit")
    assert not any(isinstance(m.cls, type) and issubclass(m.cls, BaseHTTPMiddleware)
                   for m in app.user_middleware if m.cls.__name__ in {"HostGuard", "BodyLimit"})


# ---- decide ------------------------------------------------------------------------------------

def test_decide_needs_the_token(env):
    hub, app, inc, prop, token = env
    r = call(app, "POST", f"/api/proposals/{prop.id}/decide", json={"approve": True})
    assert r.status_code == 401 and prop.status == "pending"
    r = call(app, "POST", f"/api/proposals/{prop.id}/decide", json={"approve": True},
             headers={"X-Admin-Token": token})
    assert r.status_code == 200 and r.json()["status"] == "approved"


# ---- terminal ----------------------------------------------------------------------------------

def transcript(hub):
    return [(m.sender, m.text) for m in hub.transcript]


@pytest.mark.parametrize("verb,status", [("approve", "approved"), ("deny", "denied")])
@pytest.mark.parametrize("operators", [None, {"alice", "bob"}])
def test_terminal_decisions_need_the_token_and_then_ignore_operators(verb, status, operators):
    hub, app, inc, prop, token = make(operators=operators)
    try:
        before = transcript(hub)
        r = call(app, "POST", "/api/terminal", json={"text": f"!{verb} {prop.id}"})
        assert r.status_code == 401
        assert transcript(hub) == before and prop.status == "pending"
        r = call(app, "POST", "/api/terminal", json={"text": f"!{verb} {prop.id}"},
                 headers={"X-Admin-Token": token})
        assert r.status_code == 200 and prop.status == status
        assert ("dashboard", f"!{verb} {prop.id}") in transcript(hub)
    finally:
        app.state.store.close()


def test_terminal_resolve_needs_no_token_and_keeps_the_baseline_rule():
    hub, app, inc, prop, token = make(operators=None)
    try:
        r = call(app, "POST", "/api/terminal", json={"text": f"!resolve {inc.id}"})
        assert r.status_code == 200 and hub.get_incident(inc.id).resolved
    finally:
        app.state.store.close()
    hub, app, inc, prop, token = make(operators={"bob"})
    try:
        r = call(app, "POST", "/api/terminal", json={"text": f"!resolve {inc.id}"})
        assert r.status_code == 200 and "only operators" in r.json()["replies"][0]
        assert not hub.get_incident(inc.id).resolved
    finally:
        app.state.store.close()


def test_terminal_status_needs_no_token(env):
    hub, app, *_ = env
    r = call(app, "POST", "/api/terminal", json={"text": "!status"})
    assert r.status_code == 200 and "events seen" in r.json()["replies"][0]


def test_irc_in_the_same_configuration_is_refused(env):
    hub, app, inc, prop, token = env

    async def go():
        irc = IrcServer(hub)
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"PASS {token}\r\nNICK alice\r\nJOIN #ops\r\nPRIVMSG #ops :!approve {prop.id}\r\n".encode())
            await w.drain()
            await asyncio.sleep(0.5)
            w.close()
        finally:
            server.close()
    asyncio.run(go())
    assert prop.status == "pending"


# ---- token-free routes (R6.10) -----------------------------------------------------------------

def test_token_free_routes_work_without_a_token(env):
    hub, app, inc, prop, token = env
    assert call(app, "POST", "/api/ingest", json={"source": "syslog", "line": DISK % 9}).status_code == 202
    r = call(app, "POST", "/api/proposals", json={"incident_id": inc.id, "action": "x"})
    assert r.status_code == 201
    call(app, "POST", f"/api/proposals/{prop.id}/decide", json={"approve": True}, headers={"X-Admin-Token": token})
    assert call(app, "POST", f"/api/proposals/{prop.id}/complete", json={}).status_code == 200
    assert call(app, "POST", f"/api/incidents/{inc.id}/resolve", json={}).status_code == 200
    assert call(app, "POST", "/api/irc/say", json={"text": "hi"}).status_code == 202
    assert call(app, "POST", "/api/demo/storm").status_code == 202


def test_enforcement_off_changes_nothing():
    hub, app, inc, prop, token = make(enforce=False, operators=None)
    try:
        r = call(app, "POST", "/api/terminal", json={"text": f"!approve {prop.id}"}, base="http://testserver")
        assert r.status_code == 200 and prop.status == "approved"
        r = call(app, "POST", "/api/services", json={"id": "web", "name": "W"}, base="http://testserver")
        assert r.status_code == 201
    finally:
        app.state.store.close()
