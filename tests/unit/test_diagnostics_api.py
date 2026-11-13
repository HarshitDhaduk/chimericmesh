import asyncio

import httpx
import pytest

import chimericmesh.mcp_server as mcp_server
from chimericmesh.api import create_app
from chimericmesh.diagnostics import analyze, classify, find_code_hints, signature
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.notify import is_safe_webhook_url
from chimericmesh.parsers import parse_syslog


def ev(msg, host="h1", sev=3, src="syslog"):
    return Event(source=src, host=host, app="a", severity=sev, message=msg)


def test_signature_collapses_volatile_tokens():
    assert signature("timeout to 10.0.0.5:5432 after 30s") == signature("timeout to 192.168.1.9:6000 after 12s")


def test_classify_known_and_unknown():
    assert classify("No space left on device")[0] == "disk"
    assert classify("Out of memory: kill 12")[0] == "memory"
    assert classify("hello world")[0] == "unknown"


def test_severe_single_event_is_incident():
    assert len(analyze([ev("kernel panic", sev=2)])) == 1


def test_repeated_warnings_cluster_across_sources():
    events = [ev("upstream timed out 10.0.0.%d" % i, sev=4, src=s) for i, s in enumerate(["irc", "syslog", "telnet"])]
    incs = analyze(events)
    assert len(incs) == 1 and incs[0].count == 3 and set(incs[0].sources) == {"irc", "syslog", "telnet"}


def test_info_events_never_incidents():
    assert analyze([ev("all good", sev=6)] * 10) == []


def test_find_code_hints(tmp_path):
    (tmp_path / "db.py").write_text("log.error('could not write block')\n")
    inc = analyze([ev("could not write block 42 No space left", sev=3)])[0]
    inc.sample = "could not write block 42"
    hits = find_code_hints(inc, tmp_path)
    assert hits and hits[0]["file"] == "db.py" and hits[0]["line"] == 1


def test_webhook_url_validation():
    assert is_safe_webhook_url("https://example.com/hook")
    assert not is_safe_webhook_url("file:///etc/passwd")
    assert not is_safe_webhook_url("javascript:alert(1)")


def make_client(hub):
    app = create_app(hub, start_listeners=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_api_ingest_incident_resolve_and_irc_broadcast():
    async def run():
        hub = Hub()
        sent = []

        async def fake_broadcast(channel, text):
            sent.append((channel, text))

        async with make_client(hub) as c:
            hub.irc_broadcast = fake_broadcast  # after create_app wires the real IRC server
            assert (await c.post("/api/demo/storm")).status_code == 202
            incs = (await c.get("/api/incidents")).json()
            assert {i["category"] for i in incs} >= {"disk", "network", "security"}
            assert len(sent) == len(incs)  # each incident announced exactly once
            await c.post("/api/demo/storm")
            assert len(sent) == len(incs)

            iid = incs[0]["id"]
            r = await c.post(f"/api/incidents/{iid}/resolve", json={"note": "fixed"})
            assert r.json()["resolved"] is True
            assert any("RESOLVED" in t for _, t in sent)
            assert (await c.get("/api/incidents/nope")).status_code == 404
            assert (await c.post("/api/irc/say", json={"channel": "bad chan", "text": "x"})).status_code == 422
            assert (await c.get("/api/stats")).json()["events"] >= 18

    asyncio.run(run())


def test_mcp_tools_round_trip(monkeypatch):
    async def run():
        hub = Hub()
        async with make_client(hub) as c:
            monkeypatch.setattr(mcp_server, "_client_factory", lambda: _Borrowed(c))
            await hub.ingest(parse_syslog("<27>Nov 13 11:10:08 db01 pg[1]: FATAL: No space left on device"))
            incs = await mcp_server.list_incidents()
            assert incs and incs[0]["category"] == "disk"
            one = await mcp_server.get_incident(incs[0]["id"])
            assert one["host"] == "db01"
            assert (await mcp_server.get_incident("zzz"))["error"] == 404
            assert (await mcp_server.recent_events(limit=5, source="syslog"))[0]["source"] == "syslog"
            assert (await mcp_server.announce_to_irc("hello"))["ok"] is True

    asyncio.run(run())


def test_mcp_registers_expected_tools():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"list_incidents", "get_incident", "find_code_for_incident", "recent_events",
            "announce_to_irc", "resolve_incident"} <= names


class _Borrowed:
    """Wrap a shared client so ``async with`` in the tool doesn't close it."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


# --- announcement timing, host extraction, bundled sample app ---

def test_extract_host_prefix():
    from chimericmesh.events import extract_host

    assert extract_host("web02: upstream timed out") == ("web02", "upstream timed out")
    assert extract_host("ALERT sw-core-1: Failed password") == ("sw-core-1", "Failed password")
    assert extract_host("kernel: Out of memory") == (None, "kernel: Out of memory")
    assert extract_host("ERROR: disk full") == (None, "ERROR: disk full")


def test_irc_and_telnet_use_host_from_message():
    from chimericmesh.parsers import irc_to_event, telnet_to_event

    assert irc_to_event(":oncall!u@h PRIVMSG #ops :web02: upstream timed out").host == "web02"
    assert irc_to_event(":oncall!u@h PRIVMSG #ops :hello there").host == "oncall"
    ev = telnet_to_event(b"ALERT sw-core-1: Failed password for admin", host="10.0.0.9")
    assert ev.host == "sw-core-1" and ev.severity == 3
    assert telnet_to_event(b"ERROR link flap", host="10.0.0.9").host == "10.0.0.9"


def test_announce_waits_for_threshold_then_updates_on_milestones():
    async def run():
        hub = Hub()
        sent = []

        async def fake(channel, text):
            sent.append(text)

        hub.irc_broadcast = fake
        line = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"
        for i in range(1, 13):
            await hub.ingest(parse_syslog(line % i))
            if i < 3:
                assert sent == []  # severity err, below threshold: stay quiet
        assert sent[0].startswith("[ERR] disk on db01 x3")
        assert len(sent) == 2 and sent[1].startswith("UPDATE ") and "x10" in sent[1]

    asyncio.run(run())


def test_critical_announces_immediately():
    async def run():
        hub = Hub()
        sent = []

        async def fake(channel, text):
            sent.append(text)

        hub.irc_broadcast = fake
        await hub.ingest(parse_syslog("<10>Nov 13 11:10:08 db01 kernel: kernel panic - not syncing"))
        assert len(sent) == 1 and "x1" in sent[0]

    asyncio.run(run())


def test_default_code_root_finds_sample_app_for_demo_messages():
    from chimericmesh.hub import DEFAULT_CODE_ROOT

    assert DEFAULT_CODE_ROOT.is_dir()
    hub = Hub()
    for msg in (
        "could not write block 8812: No space left on device",
        "upstream timed out connecting to 10.0.4.17:5432",
        "Failed password for admin from 203.0.113.9 port 442",
    ):
        inc = analyze([ev(msg, sev=3)])[0]
        hits = hub.code_hints(inc)
        assert hits and hits[0]["file"].endswith(".py") and "tests" not in hits[0]["file"], msg
