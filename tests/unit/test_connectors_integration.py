"""Task 8.3: the success criteria end to end (create_app, ASGITransport, a temporary database, real sockets)."""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import secrets
import socket

import httpx
import pytest

from chimericmesh import listeners
from chimericmesh.api import create_app
from chimericmesh.listeners import SYSLOG_RCVBUF, SyslogProtocol, start_syslog, start_telnet

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "chimericmesh"
NCSA = '{ip} - - [18/Nov/2026:10:00:01 +0000] "GET /orders/42 HTTP/1.1" 500 123 "-" "curl/8"\n'


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh app on a temporary database, old listeners on ephemeral ports, TCP syslog on a free port."""
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    monkeypatch.setenv("CHIMERICMESH_BIND", "127.0.0.1")
    for name in ("SYSLOG", "IRC", "TELNET"):
        monkeypatch.setenv(f"CHIMERICMESH_{name}_PORT", "0")
    port = free_port()
    monkeypatch.setenv("CHIMERICMESH_SYSLOG_TCP_PORT", str(port))
    app = create_app(start_listeners=True, enforce_access=False)
    return app, port, tmp_path


def run(app, scenario):
    """Enter the lifespan, hand the scenario an HTTP client, and close everything afterwards."""
    async def main():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await scenario(c)
    return asyncio.run(main())


async def add_service(c, sid, **body):
    r = await c.post("/api/services", json={"id": sid, "name": sid, **body})
    assert r.status_code == 201, r.text


async def add_source(c, sid, path, fmt):
    r = await c.post(f"/api/services/{sid}/log-sources", json={"path": str(path), "format": fmt})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def file_events(hub):
    return [e for e in hub.recent(500) if e.source == "file"]


# ---- Success Criterion 1 ---------------------------------------------------------------------------------


def test_sc1_ncsa_5xx_event_incident_and_status(env):
    app, _, tmp = env
    hub = app.state.hub
    log = tmp / "access.log"
    log.write_text("")

    async def scenario(c):
        await add_service(c, "web")
        await add_source(c, "web", log, "ncsa")
        await asyncio.sleep(0.8)
        with open(log, "a") as f:
            f.write(NCSA.format(ip="203.0.113.7"))
        t0 = asyncio.get_running_loop().time()
        await until(lambda: file_events(hub), timeout=2.0)
        assert asyncio.get_running_loop().time() - t0 <= 2.0
        assert file_events(hub)[0].severity == 3
        with open(log, "a") as f:
            f.write(NCSA.format(ip="203.0.113.8") + NCSA.format(ip="203.0.113.9"))
        await until(lambda: len(file_events(hub)) == 3)
        await until(lambda: hub.incidents(status="open"))
        await asyncio.sleep(0.1)                         # counters move after submit returns
        web = (await c.get("/api/services/web")).json()
        assert web["open_incidents"] == 1
        src = web["log_sources"][0]
        assert src["status"] == "running"
        assert src["lines_read"] == 3 and src["events_emitted"] == 3 and src["lines_dropped"] == 0

    run(app, scenario)


# ---- Success Criterion 2 ---------------------------------------------------------------------------------


def rename_with_retry(src, dst) -> int:
    """Rename; a rename that lands inside a read can fail once on Windows (review 8). Returns the failures."""
    failures = 0
    for _ in range(3):
        try:
            os.replace(src, dst)
            return failures
        except PermissionError:
            failures += 1
            import time
            time.sleep(0.05)
    raise AssertionError("rename blocked three times")


@pytest.mark.parametrize("mode", ["rename", "copytruncate"])
def test_sc2_rotation_delivers_every_line_once_in_order(env, mode):
    app, _, tmp = env
    hub = app.state.hub
    log = tmp / "app.log"
    log.write_text("")

    async def scenario(c):
        await add_service(c, "web")
        await add_source(c, "web", log, "plain")
        await asyncio.sleep(0.8)
        with open(log, "a") as f:
            f.write("".join(f"n{i:03d}\n" for i in range(20)))
        await until(lambda: len(file_events(hub)) == 20)
        if mode == "rename":
            assert rename_with_retry(log, str(log) + ".1") <= 1
            log.write_text("")
        else:
            with open(log, "r+b") as f:
                f.truncate(0)
        with open(log, "a") as f:
            f.write("".join(f"n{i:03d}\n" for i in range(20, 30)))
        await until(lambda: len(file_events(hub)) == 30, timeout=6.0)
        await asyncio.sleep(0.6)
        msgs = [e.message for e in sorted(file_events(hub), key=lambda e: e.id)]
        assert msgs == [f"n{i:03d}" for i in range(30)]

    run(app, scenario)


# ---- Success Criterion 3 ---------------------------------------------------------------------------------


def test_sc3_oversized_octet_frame_closes_only_that_connection(env):
    app, port, _ = env
    hub = app.state.hub

    async def scenario(c):
        server = app.state.syslog_tcp
        assert server is not None
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(b"5 hello")
        await w1.drain()
        await until(lambda: any(e.message == "hello" for e in hub.recent(20)))
        w1.write(b"102400 " + b"x" * 102400)
        await w1.drain()
        assert await asyncio.wait_for(r1.read(), 3.0) == b""          # closed by the server
        assert server.stats.dropped == 1
        w2.write(b"<14>Nov 18 10:00:01 h1 app: still here\n")
        await w2.drain()
        await until(lambda: any(e.message == "still here" for e in hub.recent(20)))
        # a fresh connection that starts with "102400 ..." is newline framing and stays open
        r3, w3 = await asyncio.open_connection("127.0.0.1", port)
        w3.write(b"102400 " + b"y" * 102400 + b"\n")
        await w3.drain()
        await until(lambda: server.stats.frames >= 3)
        w3.write(b"<14>Nov 18 10:00:02 h1 app: after big line\n")
        await w3.drain()
        await until(lambda: any(e.message == "after big line" for e in hub.recent(20)))
        assert server.stats.dropped == 1
        for w in (w2, w3):
            w.close()

    run(app, scenario)


# ---- Success Criterion 3a --------------------------------------------------------------------------------


def test_sc3a_hosts_keep_services_apart_and_client_ips_together(env):
    app, _, tmp = env
    hub = app.state.hub
    mail_a, mail_b, access = tmp / "a-mail.log", tmp / "b-mail.log", tmp / "access.log"
    for p in (mail_a, mail_b, access):
        p.write_text("")
    line = "Nov 18 10:00:01 mail01 postfix/smtp[123]: connect to mx.example timed out status=bounced\n"

    async def scenario(c):
        for sid, p, fmt in (("mail-a", mail_a, "syslog-text"), ("mail-b", mail_b, "syslog-text"),
                            ("web", access, "ncsa")):
            await add_service(c, sid)
            await add_source(c, sid, p, fmt)
        await asyncio.sleep(0.8)
        for p in (mail_a, mail_b):
            with open(p, "a") as f:
                f.write(line * 3)
        with open(access, "a") as f:
            f.write("".join(NCSA.format(ip=ip) for ip in ("198.51.100.1", "198.51.100.2", "198.51.100.3")))
        await until(lambda: len(file_events(hub)) == 9)
        await until(lambda: len(hub.incidents(status="open")) == 3)
        by = {i.service_id: i for i in hub.incidents(status="open")}
        assert set(by) == {"mail-a", "mail-b", "web"}
        assert by["mail-a"].host == "mail01/mail-a" and by["mail-b"].host == "mail01/mail-b"
        # R4.12 and O3 (the Postfix example): host from the line plus the service, app from the line
        ev = [e for e in file_events(hub) if e.service_id == "mail-a"][0]
        assert ev.app == "postfix/smtp" and ev.severity == 3 and ev.host == "mail01/mail-a"

    run(app, scenario)


# ---- Success Criterion 5 ---------------------------------------------------------------------------------


def test_sc5_udp_buffer_and_datagram_splitting_unchanged():
    assert SYSLOG_RCVBUF == 4 * 1024 * 1024
    seen = []

    class FakeHub:
        async def ingest(self, event):
            seen.append(event)
            return event

    async def main():
        SyslogProtocol(FakeHub()).datagram_received(b"<14>a h: one\n<14>a h: two\n", ("10.0.0.1", 9))
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert len(seen) == 2 and seen[0].message.endswith("one") and seen[1].message.endswith("two")


def test_sc5_existing_paths_deliver_tagged_events_and_storm_still_works(env):
    app, _, _ = env
    hub = app.state.hub

    async def scenario(c):
        await add_service(c, "legacy", hosts=["db01", "sw-core-1", "relay9"])
        udp = await start_syslog(hub, "127.0.0.1", 0)
        uport = udp.get_extra_info("sockname")[1]
        tel = await start_telnet(hub, "127.0.0.1", 0)
        tport = tel.sockets[0].getsockname()[1]
        irc = await listeners.IrcServer(hub).start("127.0.0.1", 0)
        iport = irc.sockets[0].getsockname()[1]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"<27>Nov 18 10:00:01 db01 pg[1]: udp path\n", ("127.0.0.1", uport))
            s.close()
            r, w = await asyncio.open_connection("127.0.0.1", tport)
            w.write(b"ALERT sw-core-1: telnet path\r\n")
            await w.drain()
            ir, iw = await asyncio.open_connection("127.0.0.1", iport)
            iw.write(b"NICK relay9\r\nUSER u 0 * :u\r\nJOIN #ops\r\nPRIVMSG #ops :irc path\r\n")
            await iw.drain()
            resp = await c.post("/api/ingest", json={"source": "syslog", "host": "db01",
                                                     "line": "<27>Nov 18 10:00:01 db01 pg[1]: api path"})
            assert resp.status_code == 202 and resp.json()["service_id"] == "legacy"
            want = {"udp path", "telnet path", "irc path", "api path"}
            await until(lambda: want <= {e.message for e in hub.recent(50)})
            for e in hub.recent(50):
                if e.message in want:
                    assert e.service_id == "legacy", e
            w.close()
            iw.close()
            storm = (await c.post("/api/demo/storm")).json()
            assert storm["injected"] == 9 and "at" in storm
            stormed = [e for e in hub.recent(50) if "No space left" in e.message or "upstream timed out" in e.message]
            assert stormed and all(e.service_id in (None, "unassigned") for e in stormed if e.host != "db01")
        finally:
            udp.close()
            tel.close()
            irc.close()

    run(app, scenario)


# ---- R6.2 canary -----------------------------------------------------------------------------------------


_NET_LIBS = {"httpx", "urllib", "http", "smtplib", "ftplib", "requests", "aiohttp", "socketserver"}


def test_r62_canary_never_leaves_through_a_socket(env):
    app, port, tmp = env
    hub = app.state.hub
    canary = f"CANARY-{secrets.token_hex(8)}"
    log = tmp / "c.log"
    log.write_text("")
    announced: list[str] = []
    orig_say = hub.say

    async def spy_say(channel, text):
        announced.append(text)
        await orig_say(channel, text)

    hub.say = spy_say

    async def scenario(c):
        await add_service(c, "web")
        await add_source(c, "web", log, "plain")
        r, w = await asyncio.open_connection("127.0.0.1", port)       # a TCP syslog client watches for bytes
        await asyncio.sleep(0.8)
        with open(log, "a") as f:
            f.write(f"{canary} ok\n")
        await until(lambda: any(canary in e.message for e in file_events(hub)))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(r.read(1), 0.5)                    # the TCP server writes nothing
        w.close()
        assert not any(canary in t for t in announced)               # no incident, so no announcement

    run(app, scenario)
    for path in (SRC / "connectors").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] not in _NET_LIBS, (path.name, n)
