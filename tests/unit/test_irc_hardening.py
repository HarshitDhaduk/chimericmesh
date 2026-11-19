"""IRC listener hardening: reserved nicks, PASS, operator verdicts (R9.1 to R9.5, Success Criterion 7)."""
from __future__ import annotations

import asyncio
import logging

from chimericmesh.hub import Hub
from chimericmesh.listeners import IrcServer
from chimericmesh.parsers import parse_syslog
from chimericmesh.services.access import AccessControl
from chimericmesh.services.token import TokenStore

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.lines: list[str] = []

    @classmethod
    async def connect(cls, port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return cls(r, w)

    def send(self, *lines):
        for line in lines:
            self.w.write(line.encode("utf-8") + b"\r\n")

    async def pump(self, seconds=0.4):
        end = asyncio.get_running_loop().time() + seconds
        while (left := end - asyncio.get_running_loop().time()) > 0:
            try:
                data = await asyncio.wait_for(self.r.readline(), left)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            self.lines.append(data.decode("utf-8", "replace"))
        return self.lines

    @property
    def text(self):
        return "".join(self.lines)

    def close(self):
        self.w.close()


async def setup(enforce=True, operators=None, tokens=True):
    hub = Hub(enforce_access=enforce)
    hub.operators = operators
    ts = TokenStore.open(None, in_memory=True)
    if enforce and tokens:
        hub.access = AccessControl(enforce=True, tokens=ts, allowed_hosts=frozenset({"localhost"}), audit=None)
    irc = IrcServer(hub)
    server = await irc.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for i in range(3):
        await hub.ingest(parse_syslog(DISK % i))
    inc = hub.incidents()[0]
    prop = await hub.create_proposal(inc.id, "rotate logs")
    return hub, inc, prop, server, port, ts.current()


async def register(port, nick, token=None, join=True):
    c = await Client.connect(port)
    if token is not None:
        c.send(f"PASS {token}")
    c.send(f"NICK {nick}", f"USER {nick} 0 * :{nick}")
    if join:
        c.send("JOIN #ops")
    await c.pump(0.3)
    return c


def run(coro):
    return asyncio.run(coro)


def test_reserved_nicks_get_433_at_registration():
    async def go():
        hub, inc, prop, server, port, token = await setup()
        try:
            for nick in ("dashboard", "Dashboard", "ChimericBot"):
                c = await Client.connect(port)
                c.send(f"NICK {nick}")
                await c.pump(0.3)
                assert " 433 " in c.text and " 001 " not in c.text
                c.send("JOIN #ops", "PRIVMSG #ops :hello")
                await c.pump(0.3)
                assert not any(e.message == "hello" for e in hub.events)
                c.close()
        finally:
            server.close()
    run(go())


def test_registered_client_cannot_switch_to_a_reserved_nick():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            c = await register(port, "op1", token)
            c.send("NICK dashboard")
            await c.pump(0.3)
            assert " 433 " in c.text
            c.send(f"PRIVMSG #ops :!approve {prop.id}")
            await c.pump(0.4)
            assert prop.status == "approved" and prop.decided_by == "op1"
            c.close()
        finally:
            server.close()
    run(go())


def test_operator_with_pass_can_approve_and_without_pass_cannot():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            nopass = await register(port, "op1")
            nopass.send(f"PRIVMSG #ops :!approve {prop.id}")
            await nopass.pump(0.4)
            assert "PASS" in nopass.text and prop.status == "pending"
            ok = await register(port, "op1", token)
            ok.send(f"PRIVMSG #ops :!approve {prop.id}")
            await ok.pump(0.4)
            assert prop.status == "approved"
            nopass.close()
            ok.close()
        finally:
            server.close()
    run(go())


def test_wrong_pass_gets_464_and_client_still_reads():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            c = await register(port, "op1", "wrong-token")
            assert " 464 " in c.text
            c.send("PRIVMSG #ops :!status")
            await c.pump(0.4)
            assert "events seen" in c.text
            c.send(f"PRIVMSG #ops :!approve {prop.id}")
            await c.pump(0.4)
            assert prop.status == "pending"
            c.close()
        finally:
            server.close()
    run(go())


def test_pass_after_registration_is_ignored():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            c = await register(port, "op1")
            c.send(f"PASS {token}", f"PRIVMSG #ops :!approve {prop.id}")
            await c.pump(0.4)
            assert prop.status == "pending"
            c.close()
        finally:
            server.close()
    run(go())


def test_operators_unset_names_the_setting_for_every_operator_command():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators=None)
        try:
            c = await register(port, "alice", token)
            for cmd in (f"!approve {prop.id}", f"!deny {prop.id}", f"!resolve {inc.id}"):
                c.send(f"PRIVMSG #ops :{cmd}")
            await c.pump(0.5)
            assert c.text.count("CHIMERICMESH_OPERATORS") == 3
            assert prop.status == "pending" and not hub.get_incident(inc.id).resolved
            c.close()
        finally:
            server.close()
    run(go())


def test_non_operator_with_correct_pass_is_refused_and_read_only_works_for_everyone():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            c = await register(port, "mallory", token)
            c.send(f"PRIVMSG #ops :!approve {prop.id}", "PRIVMSG #ops :!incidents",
                   "PRIVMSG #ops :!help", "PRIVMSG #ops :!status")
            await c.pump(0.5)
            assert "only operators" in c.text and inc.id in c.text and "!incidents" in c.text
            assert "events seen" in c.text and prop.status == "pending"
            c.close()
        finally:
            server.close()
    run(go())


def test_pass_value_never_appears_anywhere(caplog):
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            with caplog.at_level(logging.DEBUG):
                a = await register(port, "op1", token)
                b = await register(port, "watcher")
                a.send(f"PRIVMSG #ops :!approve {prop.id}")
                await a.pump(0.3)
                await b.pump(0.3)
            seen = a.text + b.text
            assert token not in seen
            assert token not in caplog.text
            assert all(token not in m.text for m in hub.transcript)
            a.close()
            b.close()
        finally:
            server.close()
    run(go())


def test_hostile_nicks_are_cut_and_echoed_clean():
    async def go():
        hub, inc, prop, server, port, token = await setup()
        try:
            c = await Client.connect(port)
            c.send("NICK " + "n" * 200)
            await c.pump(0.3)
            c.send("JOIN #ops")
            await c.pump(0.3)
            assert ":" + "n" * 30 + "!u@h JOIN" in c.text and "n" * 31 not in c.text
            d = await Client.connect(port)
            d.w.write(b"NICK ab\x00cd\r\nJOIN #ops\r\n")
            await d.pump(0.3)
            assert "\x00" not in d.text
            assert all(line.endswith("\r\n") and "\r" not in line[:-2] and "\n" not in line[:-2]
                       and "\x00" not in line for line in d.lines + c.lines)
            c.close()
            d.close()
        finally:
            server.close()
    run(go())


def test_success_criterion_7_second_dashboard_and_passless_operator_cannot_approve():
    async def go():
        hub, inc, prop, server, port, token = await setup(operators={"op1"})
        try:
            fake = await Client.connect(port)
            fake.send("NICK dashboard", "JOIN #ops", f"PRIVMSG #ops :!approve {prop.id}")
            await fake.pump(0.4)
            passless = await register(port, "op1")
            passless.send(f"PRIVMSG #ops :!approve {prop.id}")
            await passless.pump(0.4)
            assert prop.status == "pending"
            fake.close()
            passless.close()
        finally:
            server.close()
    run(go())


def test_enforcement_off_keeps_baseline_behaviour():
    async def go():
        hub, inc, prop, server, port, token = await setup(enforce=False, operators=None)
        try:
            c = await Client.connect(port)
            c.send("NICK dashboard", "JOIN #ops")
            await c.pump(0.3)
            assert " 001 " in c.text and " 433 " not in c.text
            c.send("PASS whatever", f"PRIVMSG #ops :!approve {prop.id}")
            await c.pump(0.4)
            assert " 464 " not in c.text and prop.status == "approved"      # operators None: any nick
            c.close()
        finally:
            server.close()
    run(go())
