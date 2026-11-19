"""Success Criteria of the services module end to end through ``create_app`` on a temporary database."""
from __future__ import annotations

import asyncio
import inspect
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import chimericmesh.mcp_server as mcp_server
from chimericmesh.api import create_app
from chimericmesh.listeners import IrcServer, start_syslog
from chimericmesh.parsers import parse_syslog
from chimericmesh.services.limiter import ReduceAuditLimiter

BASE = "http://127.0.0.1:8000"
DEMO = "<131>Nov 19 10:00:0%d web02 proxy-worker[4242]: upstream timed out connecting to 127.0.0.1:19099"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "data" / "mesh.db"))
    monkeypatch.delenv("CHIMERICMESH_OPERATORS", raising=False)
    return tmp_path


class App:
    """One app instance (enforcement on, as from ``python -m chimericmesh``) driven over ASGI."""

    def __init__(self):
        self.app = create_app(start_listeners=False)
        self.hub = self.app.state.hub
        self.token = self.app.state.access.tokens.current()

        async def quiet(channel, text):
            pass
        self.hub.irc_broadcast = quiet

    async def request(self, method, url, *, authed=False, **kw):
        headers = dict(kw.pop("headers", {}))
        if authed:
            headers["X-Admin-Token"] = self.token
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 4000))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            return await c.request(method, url, headers=headers, **kw)

    def call(self, *a, **kw):
        return asyncio.run(self.request(*a, **kw))

    def close(self):
        async def down():
            await self.hub.aclose()
        asyncio.run(down())
        self.app.state.store.close()


def test_sc1_registered_service_tags_events_and_incident_through_rest_and_udp(db):
    folder = db / "proxy-worker-src"
    folder.mkdir()
    a = App()
    try:
        r = a.call("POST", "/api/services", authed=True,
                   json={"id": "proxy-worker", "name": "Proxy worker", "app_names": ["proxy-worker"],
                         "code_folder": str(folder)})
        assert r.status_code == 201

        async def traffic():
            for i in range(2):
                assert (await a.request("POST", "/api/ingest",
                                        json={"source": "syslog", "line": DEMO % i})).status_code == 202
            transport = await start_syslog(a.hub, "127.0.0.1", 0)
            port = transport.get_extra_info("socket").getsockname()[1]
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto((DEMO % 2).encode(), ("127.0.0.1", port))
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if a.hub.total_events >= 3:
                        break
            finally:
                sender.close()
                transport.close()
            return (await a.request("GET", "/api/events?limit=10")).json(), \
                (await a.request("GET", "/api/incidents")).json(), \
                (await a.request("GET", "/api/services/proxy-worker")).json()

        events, incidents, status = asyncio.run(traffic())
        assert len(events) == 3 and {e["service_id"] for e in events} == {"proxy-worker"}
        assert incidents and incidents[0]["service_id"] == "proxy-worker"
        assert status["state"] == "active" and status["error_rate"] > 0 and status["open_incidents"] == 1
    finally:
        a.close()


def test_sc2_create_without_the_token_is_401_and_nothing_exists(db):
    a = App()
    try:
        assert a.call("POST", "/api/services", json={"id": "web", "name": "W"}).status_code == 401
        assert a.call("GET", "/api/services/web").status_code == 404
        assert a.call("POST", "/api/services", authed=True, json={"id": "web", "name": "W"}).status_code == 201
    finally:
        a.close()


def test_sc3_foreign_host_is_403(db):
    a = App()
    try:
        assert a.call("GET", "/api/stats", headers={"Host": "evil.example"}).status_code == 403
    finally:
        a.close()


def test_sc8_same_token_file_from_two_working_directories(db, monkeypatch):
    paths = []
    for name in ("one", "two"):
        cwd = db / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        a = App()
        paths.append((a.app.state.access.token_path, a.token))
        a.close()
    assert paths[0] == paths[1]
    assert paths[0][0] == Path(db / "cm-home" / "admin.token")
    assert not (db / "one" / "admin.token").exists() and not (db / "two" / "admin.token").exists()


def test_sc9_terminal_with_token_approves_with_operators_unset_and_irc_is_refused(db):
    a = App()
    try:
        async def seed():
            for i in range(3):
                await a.hub.ingest(parse_syslog(DEMO % i))
            return await a.hub.create_proposal(a.hub.incidents()[0].id, "raise the timeout")
        prop = asyncio.run(seed())
        assert a.hub.operators is None
        assert a.call("POST", "/api/terminal", json={"text": f"!approve {prop.id}"}).status_code == 401

        async def irc_try():
            server = await IrcServer(a.hub).start("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(f"PASS {a.token}\r\nNICK alice\r\nJOIN #ops\r\nPRIVMSG #ops :!approve {prop.id}\r\n".encode())
                await w.drain()
                await asyncio.sleep(0.4)
                w.close()
            finally:
                server.close()
        asyncio.run(irc_try())
        assert a.hub.get_proposal(prop.id).status == "pending"
        r = a.call("POST", "/api/terminal", authed=True, json={"text": f"!approve {prop.id}"})
        assert r.status_code == 200 and a.hub.get_proposal(prop.id).status == "approved"
    finally:
        a.close()


def test_sc10_dependency_direction_test_passes():
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          str(ROOT / "tests" / "unit" / "test_dependency_direction.py")],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout[-2000:]


def test_sc11_reduce_limiter_ten_rows_then_one_coalesced_row_and_never_refuses(db):
    a = App()
    try:
        clock = [1000.0]
        limiter = ReduceAuditLimiter(a.app.state.store.audit, now=lambda: clock[0])

        async def go():
            results = [await limiter.record("127.0.0.1", actor="rest:127.0.0.1", action="rule_paused",
                                            subject="r1") for _ in range(11)]
            await limiter.aclose()
            return results, await a.app.state.store.audit.read(limit=50)
        results, rows = asyncio.run(go())
        assert results == [True] * 10 + [False]
        assert sum(r["action"] == "rule_paused" for r in rows) == 10
        coalesced = [r for r in rows if r["action"] == "reduce_ops_suppressed"]
        assert len(coalesced) == 1 and coalesced[0]["detail"] == "count=1"
    finally:
        a.close()


def test_restart_keeps_services_and_retained_event_counts(db):
    a = App()
    a.call("POST", "/api/services", authed=True, json={"id": "proxy-worker", "name": "P",
                                                        "app_names": ["proxy-worker"]})
    for i in range(4):
        a.call("POST", "/api/ingest", json={"source": "syslog", "line": DEMO % i})
    a.close()
    b = App()
    try:
        later = time.time() + 400                     # the formula: idle once the newest event is over 5 minutes old
        b.hub._clock = lambda: later
        s = b.call("GET", "/api/services/proxy-worker").json()
        assert s["id"] == "proxy-worker" and s["app_names"] == ["proxy-worker"]
        assert s["state"] == "idle" and s["events_total"] == 4
    finally:
        b.close()


def test_mcp_tools_take_no_token_and_never_return_it(db, monkeypatch):
    a = App()
    try:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        for tool in tools:
            params = set(inspect.signature(getattr(mcp_server, tool.name)).parameters)
            assert not any("token" in p.lower() for p in params), tool.name
            assert "token" not in str(tool.input_schema).lower(), tool.name

        class Borrowed:
            def __init__(self, client):
                self.client = client

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, *exc):
                return False

        async def go():
            for i in range(3):
                await a.hub.ingest(parse_syslog(DEMO % i))
            transport = httpx.ASGITransport(app=a.app, client=("127.0.0.1", 1))
            async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
                monkeypatch.setattr(mcp_server, "_client_factory", lambda: Borrowed(c))
                iid = (await mcp_server.list_incidents())[0]["id"]
                outputs = [
                    await mcp_server.list_incidents(), await mcp_server.get_incident(iid),
                    await mcp_server.find_code_for_incident(iid), await mcp_server.recent_events(),
                    await mcp_server.announce_to_irc("hi"), await mcp_server.propose_remediation(iid, "x"),
                    await mcp_server.get_proposal("p1"), await mcp_server.list_proposals(),
                    await mcp_server.complete_proposal("p1"), await mcp_server.resolve_incident(iid),
                ]
            return outputs
        outputs = asyncio.run(go())
        assert all(a.token not in str(o) for o in outputs)
    finally:
        a.close()
