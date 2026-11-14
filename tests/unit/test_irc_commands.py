"""IRC commands, operator allowlist, remediation proposals, REST and MCP wiring."""
import asyncio

import httpx
import pytest

import chimericmesh.mcp_server as mcp_server
from chimericmesh.api import create_app
from chimericmesh.commands import Command, handle_command, parse_command
from chimericmesh.hub import MAX_PROPOSALS, Hub, parse_operators
from chimericmesh.listeners import IrcServer
from chimericmesh.parsers import parse_syslog

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


def fake_hub(operators=None):
    hub = Hub()
    hub.operators = operators
    hub.sent = []

    async def fake(channel, text):
        hub.sent.append((channel, text))

    hub.irc_broadcast = fake
    return hub


async def seed(hub, n=3):
    for i in range(n):
        await hub.ingest(parse_syslog(DISK % i))
    return hub.incidents()[0]


def run_cmd(hub, text, nick="alice", channel="#ops"):
    return asyncio.run(handle_command(hub, nick, channel, parse_command(text)))


# --- parse_command ---

def test_parse_command_basics():
    assert parse_command("!help") == Command("help", ())
    assert parse_command("!EXPLAIN  abc123 ") == Command("explain", ("abc123",))
    assert parse_command("!resolve x a b c d e f g").args == ("x", "a", "b", "c", "d")  # max 5
    assert parse_command("hello") is None
    assert parse_command("") is None
    assert parse_command("!") is None
    assert parse_command(" !help") is None


def test_parse_command_limits_considered_text():
    cmd = parse_command("!resolve x " + "y" * 1000)
    assert cmd is not None and len(cmd.args[1]) <= 200


# --- replies ---

def test_help_status_and_unknown():
    hub = fake_hub()
    assert any("!incidents" in r for r in run_cmd(hub, "!help"))
    assert "events seen: 0" in run_cmd(hub, "!status")[0]
    out = run_cmd(hub, "!foo")
    assert out[0].startswith("[mesh]") and "!help" in out[0]
    assert hub.sent and all(c == "#ops" for c, _ in hub.sent)


def test_incidents_and_explain():
    async def go():
        hub = fake_hub()
        inc = await seed(hub)
        replies = await handle_command(hub, "alice", "#ops", Command("incidents"))
        assert replies == [f"[mesh] {inc.id} disk db01 x3 err"]
        exp = await handle_command(hub, "alice", "#ops", Command("explain", (inc.id,)))
        assert inc.hypothesis in exp[0] and exp[1].startswith("[mesh] 1. ") and len(exp) <= 5
        miss = await handle_command(hub, "alice", "#ops", Command("explain", ("nope",)))
        assert "no incident" in miss[0]
        assert "usage" in (await handle_command(hub, "alice", "#ops", Command("explain")))[0]
    asyncio.run(go())


def test_incidents_capped_at_five_and_empty_message():
    async def go():
        hub = fake_hub()
        assert "no open incidents" in (await handle_command(hub, "a", "#ops", Command("incidents")))[0]
        for h in range(8):
            for i in range(3):
                await hub.ingest(parse_syslog(DISK.replace("db01", f"db{h:02d}") % i))
        assert len(await handle_command(hub, "a", "#ops", Command("incidents"))) == 5
    asyncio.run(go())


def test_resolve_command_and_non_channel_target_replies_to_ops():
    async def go():
        hub = fake_hub()
        inc = await seed(hub)
        out = await handle_command(hub, "alice", "bob", Command("resolve", (inc.id, "fixed")))
        assert out == [f"[mesh] resolved {inc.id}"]
        assert hub.get_incident(inc.id).resolved
        assert {c for c, _ in hub.sent} == {"#ops"}
        assert "no open incidents" in (await handle_command(hub, "a", "#ops", Command("incidents")))[0]
    asyncio.run(go())


def test_proposals_approve_deny_commands():
    async def go():
        hub = fake_hub()
        inc = await seed(hub)
        assert "no pending" in (await handle_command(hub, "a", "#ops", Command("proposals")))[0]
        p1 = await hub.create_proposal(inc.id, "rotate logs")
        p2 = await hub.create_proposal(inc.id, "drop tables")
        listing = await handle_command(hub, "a", "#ops", Command("proposals"))
        assert len(listing) == 2 and "rotate logs" in listing[0]
        assert (await handle_command(hub, "alice", "#ops", Command("approve", ("P1",))))[0].endswith("p1 approved")
        assert (await handle_command(hub, "alice", "#ops", Command("deny", ("p2",))))[0].endswith("p2 denied")
        assert (p1.status, p1.decided_by, p2.status) == ("approved", "alice", "denied")
        again = await handle_command(hub, "alice", "#ops", Command("approve", ("p1",)))
        assert "not pending" in again[0]
        assert "no proposal" in (await handle_command(hub, "alice", "#ops", Command("deny", ("p99",))))[0]
        assert "usage" in (await handle_command(hub, "alice", "#ops", Command("approve")))[0]
        texts = [t for _, t in hub.sent]
        assert any(t.startswith("PROPOSAL p1 for ") for t in texts)
        assert "p1 APPROVED by alice" in texts and "p2 DENIED by alice" in texts
    asyncio.run(go())


# --- operators ---

def test_parse_operators():
    assert parse_operators(None) is None
    assert parse_operators(" , ") is None
    assert parse_operators("Alice, BOB") == {"alice", "bob"}


def test_operator_env_var(monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_OPERATORS", "Alice,bob")
    assert Hub().operators == {"alice", "bob"}
    monkeypatch.delenv("CHIMERICMESH_OPERATORS")
    assert Hub().operators is None


def test_operator_allow_deny_and_case_insensitive():
    async def go():
        hub = fake_hub(operators={"alice"})
        inc = await seed(hub)
        p = await hub.create_proposal(inc.id, "restart")
        out = await handle_command(hub, "mallory", "#ops", Command("approve", (p.id,)))
        assert "only operators" in out[0] and p.status == "pending"
        assert "only operators" in (await handle_command(hub, "mallory", "#ops", Command("resolve", (inc.id,))))[0]
        assert not hub.get_incident(inc.id).resolved
        # read-only commands stay open to everyone
        assert "events seen" in (await handle_command(hub, "mallory", "#ops", Command("status")))[0]
        out = await handle_command(hub, "ALICE", "#ops", Command("approve", (p.id,)))
        assert out[0].endswith("p1 approved") and p.decided_by == "ALICE"
    asyncio.run(go())


# --- proposal lifecycle ---

def test_proposal_lifecycle_and_invalid_transitions():
    async def go():
        hub = fake_hub()
        inc = await seed(hub)
        assert await hub.create_proposal("nope", "x") is None
        p = await hub.create_proposal(inc.id, "a" * 300, "d" * 900)
        assert p.id == "p1" and len(p.action) == 200 and len(p.details) == 500
        assert await hub.complete(p.id, "early") is None  # not approved yet
        assert await hub.decide("zz", True, "x") is None
        assert (await hub.decide(p.id, True, "bob")).status == "approved"
        assert await hub.decide(p.id, True, "bob") is None  # approve twice
        assert await hub.decide(p.id, False, "bob") is None
        assert (await hub.complete(p.id, "done it")).status == "done"
        assert await hub.complete(p.id, "again") is None
        assert [x.id for x in hub.list_proposals("done")] == ["p1"] and hub.list_proposals("pending") == []
        assert "p1 DONE: done it" in [t for _, t in hub.sent]
        assert (await hub.create_proposal(inc.id, "x")).id == "p2"
    asyncio.run(go())


def test_proposals_bounded_oldest_first():
    async def go():
        hub = fake_hub()
        inc = await seed(hub)
        for _ in range(MAX_PROPOSALS + 5):
            await hub.create_proposal(inc.id, "x")
        ids = [p.id for p in hub.list_proposals()]
        assert len(ids) == MAX_PROPOSALS and ids[0] == "p6" and hub.get_proposal("p1") is None
    asyncio.run(go())


# --- end to end over a real in-process IRC server ---

def _serve():
    hub = Hub()
    hub.operators = None
    return hub, IrcServer(hub)


async def _read_until(reader, needle, timeout=3.0):
    lines = []
    async def pump():
        while True:
            line = (await reader.readline()).decode()
            lines.append(line)
            if needle in line:
                return
    await asyncio.wait_for(pump(), timeout)
    return lines


def test_commands_not_ingested_and_reply_over_socket():
    async def run():
        hub, irc = _serve()
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            for _ in range(3):
                await hub.ingest(parse_syslog("<27>Nov 13 11:10:08 web02 app[1]: upstream timed out connecting to 10.0.4.17:5432"))
            before = len(hub.events)
            inc = hub.incidents()[0]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"NICK op1\r\nUSER o 0 * :o\r\nJOIN #ops\r\n")
            w.write(b"!incidents\r\nPRIVMSG #ops :!incidents\r\n")
            await w.drain()
            lines = await _read_until(r, inc.id)
            reply = [ln for ln in lines if inc.id in ln][0]
            assert reply.startswith(":ChimericBot!bot@") and "PRIVMSG #ops :[mesh] " in reply
            assert "network" in reply and "x3" in reply
            # a plain chat line still ingests; command lines never do
            w.write(b"PRIVMSG #ops :!status\r\nPRIVMSG #ops :!\r\nPRIVMSG #ops :hello there\r\n")
            await w.drain()
            await asyncio.sleep(0.3)
            assert len(hub.events) == before + 1
            assert hub.events[-1].message == "hello there"
            w.close()
        finally:
            server.close()
    asyncio.run(run())


def test_unregistered_client_cannot_run_commands():
    async def run():
        hub, irc = _serve()
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"JOIN #ops\r\nPRIVMSG #ops :!status\r\n")
            await w.drain()
            await asyncio.sleep(0.3)
            assert len(hub.events) == 0
            with pytest.raises(asyncio.TimeoutError):
                await _read_until(r, "[mesh]", timeout=0.4)
            w.close()
        finally:
            server.close()
    asyncio.run(run())


# --- REST ---

def make_client(hub):
    app = create_app(hub, start_listeners=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_rest_proposal_endpoints():
    async def run():
        hub = Hub()
        sent = []

        async def fake(channel, text):
            sent.append(text)

        async with make_client(hub) as c:
            hub.irc_broadcast = fake  # after create_app wires the real IRC server
            for i in range(3):
                await hub.ingest(parse_syslog(DISK % i))
            iid = hub.incidents()[0].id
            assert (await c.post("/api/proposals", json={"incident_id": "zz", "action": "x"})).status_code == 404
            assert (await c.post("/api/proposals", json={"incident_id": iid, "action": ""})).status_code == 422
            assert (await c.post("/api/proposals", json={"incident_id": iid, "action": "x" * 201})).status_code == 422
            r = await c.post("/api/proposals", json={"incident_id": iid, "action": "rotate logs"})
            assert r.status_code == 201 and r.json()["id"] == "p1" and r.json()["status"] == "pending"
            assert sent[-1].startswith("PROPOSAL p1 for " + iid) and "!approve p1" in sent[-1]
            assert (await c.get("/api/proposals/p1")).json()["action"] == "rotate logs"
            assert (await c.get("/api/proposals/p9")).status_code == 404
            assert len((await c.get("/api/proposals", params={"status": "pending"})).json()) == 1
            assert (await c.get("/api/proposals", params={"status": "bogus"})).status_code == 422
            assert (await c.post("/api/proposals/p1/complete", json={"note": "x"})).status_code == 409
            assert (await c.post("/api/proposals/p9/decide", json={"approve": True})).status_code == 404
            d = await c.post("/api/proposals/p1/decide", json={"approve": True})
            assert d.status_code == 200 and d.json()["status"] == "approved" and d.json()["decided_by"] == "api"
            assert (await c.post("/api/proposals/p1/decide", json={"approve": False})).status_code == 409
            assert (await c.post("/api/proposals/p1/complete", json={"note": "n" * 301})).status_code == 422
            done = await c.post("/api/proposals/p1/complete", json={"note": "rotated"})
            assert done.json()["status"] == "done" and done.json()["result_note"] == "rotated"
            assert (await c.post("/api/proposals/p1/complete", json={"note": "x"})).status_code == 409
            assert "p1 APPROVED by api" in sent and "p1 DONE: rotated" in sent
    asyncio.run(run())


# --- MCP ---

class _Borrowed:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


def test_mcp_proposal_tools_round_trip(monkeypatch):
    async def run():
        hub = Hub()
        async with make_client(hub) as c:
            hub.irc_broadcast = None
            monkeypatch.setattr(mcp_server, "_client_factory", lambda: _Borrowed(c))
            for i in range(3):
                await hub.ingest(parse_syslog(DISK % i))
            iid = hub.incidents()[0].id
            assert (await mcp_server.propose_remediation("zz", "x"))["error"] == 404
            p = await mcp_server.propose_remediation(iid, "rotate logs", "details")
            assert p["id"] == "p1" and p["status"] == "pending"
            assert (await mcp_server.get_proposal("p1"))["status"] == "pending"
            assert (await mcp_server.get_proposal("p7"))["error"] == 404
            assert (await mcp_server.complete_proposal("p1"))["error"] == 409
            await hub.decide("p1", True, "alice")
            assert (await mcp_server.get_proposal("p1"))["status"] == "approved"
            assert len(await mcp_server.list_proposals("approved")) == 1
            assert await mcp_server.list_proposals("pending") == []
            assert len(await mcp_server.list_proposals()) == 1
            assert (await mcp_server.complete_proposal("p1", "ok"))["status"] == "done"
    asyncio.run(run())


def test_mcp_registers_proposal_tools_and_describes_loop():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"propose_remediation", "get_proposal", "list_proposals", "complete_proposal"} <= names
    instructions = mcp_server.mcp.instructions
    assert "propose_remediation" in instructions and "approved" in instructions
