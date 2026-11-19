"""Asyncio listeners speaking the legacy protocols: Syslog/UDP, Telnet/TCP, IRC/TCP."""

from __future__ import annotations

import asyncio
import logging
import re
import socket

from .commands import handle_command, parse_command
from .hub import Hub
from .services.access import OperatorVerdict, is_reserved_nick
from .parsers import format_privmsg, irc_to_event, parse_irc_line, parse_syslog, telnet_to_event

log = logging.getLogger("chimericmesh.listeners")
MAX_READ = 4096
MAX_PASS = 200
_CTRL = re.compile(r"[\r\n\x00]")


class SyslogProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def datagram_received(self, data: bytes, addr) -> None:
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines() or [text]:
            if line.strip():
                event = parse_syslog(line, default_host=addr[0])
                asyncio.ensure_future(self.hub.ingest(event))


SYSLOG_RCVBUF = 4 * 1024 * 1024  # absorb bursts; the OS default drops datagrams early


async def start_syslog(hub: Hub, host: str, port: int):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SYSLOG_RCVBUF)
        sock.bind((host, port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    transport, _ = await loop.create_datagram_endpoint(lambda: SyslogProtocol(hub), sock=sock)
    return transport


async def start_telnet(hub: Hub, host: str, port: int) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("telnet-peer",))[0]
        writer.write(b"ChimericMesh telnet intake. Type a log line, ENTER to send.\r\n")
        try:
            while data := await reader.readline():
                event = telnet_to_event(data[:MAX_READ], host=str(peer))
                if event:
                    await hub.ingest(event)
                    writer.write(b"ack\r\n")
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


class IrcServer:
    """A tiny IRC server: registration, JOIN, PING, PRIVMSG ingestion and broadcast."""

    def __init__(self, hub: Hub, name: str = "chimeric.mesh") -> None:
        self.hub, self.name = hub, name
        self.clients: dict[asyncio.StreamWriter, dict] = {}
        hub.irc_broadcast = self.broadcast

    async def broadcast(self, channel: str, text: str) -> None:
        for line in format_privmsg(channel, text):
            for writer, info in list(self.clients.items()):
                if channel in info["channels"]:
                    self._send(writer, f":ChimericBot!bot@{self.name} {line}")

    def _send(self, writer: asyncio.StreamWriter, line: str) -> None:
        try:
            writer.write((line[:510] + "\r\n").encode())
        except (ConnectionError, RuntimeError):
            self.clients.pop(writer, None)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        info = {"nick": "*", "channels": set(), "registered": False, "pass_ok": False}
        self.clients[writer] = info
        try:
            while data := await reader.readline():
                line = data[:MAX_READ].decode("utf-8", errors="replace")
                msg = parse_irc_line(line)
                if msg is None:
                    continue
                await self._dispatch(writer, info, msg, line)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.pop(writer, None)
            writer.close()

    async def _dispatch(self, writer, info, msg, line: str) -> None:
        cmd, params = msg.command, msg.params
        enforce = self.hub.enforce_access
        if cmd == "PASS" and params and enforce and not info["registered"]:
            # The value is compared and dropped: never stored, echoed, logged or recorded.
            access = self.hub.access
            value = params[0]
            info["pass_ok"] = bool(access is not None and len(value) <= MAX_PASS and access.verify_pass(value))
            if not info["pass_ok"]:
                self._send(writer, f":{self.name} 464 {info['nick']} :Password incorrect")
        elif cmd == "NICK" and params:
            nick = _CTRL.sub("", params[0])[:30]
            if not nick:
                self._send(writer, f":{self.name} 431 {info['nick']} :No nickname given")
                return
            if enforce and is_reserved_nick(nick):
                self._send(writer, f":{self.name} 433 {info['nick']} {nick} :Nickname is already in use")
                return
            info["nick"] = nick
            if not info["registered"]:
                info["registered"] = True
                self._send(writer, f":{self.name} 001 {info['nick']} :Welcome to ChimericMesh")
        elif cmd == "JOIN" and params:
            for chan in params[0].split(","):
                if chan.startswith("#"):
                    info["channels"].add(chan)
                    self._send(writer, f":{info['nick']}!u@h JOIN {chan}")
        elif cmd == "PING":
            self._send(writer, f":{self.name} PONG {self.name} :{params[0] if params else ''}")
        elif cmd in {"PRIVMSG", "NOTICE"}:
            # Never trust a client-supplied prefix: the sender is the registered nick.
            if info["registered"] and len(params) >= 2:
                if params[-1].startswith("!"):
                    # Commands are operator input, not log data: run, never ingest.
                    command = parse_command(params[-1])
                    if command is not None:
                        self.hub.record(info["nick"], params[-1], "user")
                        await handle_command(self.hub, info["nick"], params[0], command,
                                             verdict=self._verdict(info))
                    return
                tagged = f":{info['nick']}!u@h {cmd} {params[0]} :{params[-1]}"
                event = irc_to_event(tagged)
                if event:
                    await self.hub.ingest(event)
        elif cmd == "QUIT":
            raise ConnectionError

    def _verdict(self, info: dict) -> OperatorVerdict | None:
        """The operator verdict for this connection; ``None`` (baseline) while enforcement is off."""
        access = self.hub.access
        if not self.hub.enforce_access or access is None:
            return None  # off: baseline rule. On without access: handle_command fails closed.
        return access.operator_verdict(info["nick"], operators=self.hub.operators, pass_ok=info["pass_ok"])

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port)
