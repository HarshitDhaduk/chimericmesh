"""Syslog over TCP: octet-counted and newline-framed messages, parsed like the UDP listener's."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from ..parsers.syslog import parse_syslog
from .config import TcpConfig
from .framing import FrameDecoder, FrameError
from .submit import Submit

log = logging.getLogger("chimericmesh.connectors")

_WARN_EVERY = 10.0


@dataclass(slots=True)
class ListenerStats:
    accepted: int = 0
    refused: int = 0
    active: int = 0
    frames: int = 0
    dropped: int = 0
    framing_errors: int = 0
    idle_closed: int = 0


class TcpSyslogServer:
    def __init__(self, submit: Submit, cfg: TcpConfig | None = None) -> None:
        self._submit = submit
        self._cfg = cfg or TcpConfig()
        self.stats = ListenerStats()
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._last_warn = -_WARN_EVERY
        self._closed = False

    async def start(self, host: str, port: int) -> bool:
        try:
            self._server = await asyncio.start_server(self._handle, host, port)
        except OSError as exc:
            log.warning("syslog TCP port %d is not available (%s); TCP syslog is off", port, exc)
            self._server = None
            return False
        self.port = self._server.sockets[0].getsockname()[1]
        self._closed = False
        return True

    async def aclose(self, timeout: float = 1.0) -> None:
        if self._closed:
            return
        self._closed = True
        server, self._server = self._server, None
        self.port = None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)
        if server is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(server.wait_closed(), timeout)

    def _warn(self, message: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_warn >= _WARN_EVERY:
            self._last_warn = now
            log.warning(message, *args)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stats = self.stats
        if stats.active >= self._cfg.max_connections:
            stats.refused += 1
            writer.close()
            return
        stats.accepted += 1
        stats.active += 1
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        peer = writer.get_extra_info("peername")
        host = str(peer[0]) if peer else "unknown"
        decoder = FrameDecoder(self._cfg.max_frame)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(self._cfg.read_size), self._cfg.idle_timeout)
                except asyncio.TimeoutError:
                    stats.idle_closed += 1
                    break
                except (ConnectionError, OSError):
                    break
                if not data:
                    for frame in decoder.finish():
                        await self._deliver(frame, host)
                    break
                failed = False
                for item in decoder.feed(data):
                    if isinstance(item, FrameError):
                        stats.framing_errors += 1
                        stats.dropped += 1
                        self._warn("syslog TCP frame error from %s: %s", host, item.reason)
                        failed = True
                        break
                    await self._deliver(item, host)
                if failed:
                    break
        finally:
            stats.active -= 1
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    async def _deliver(self, frame: bytes, host: str) -> None:
        try:
            text = frame.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
            event = parse_syslog(text, default_host=host)
            await self._submit(event, None)
            self.stats.frames += 1
        except Exception as exc:  # R1.3: one bad frame never stops the connection
            self.stats.dropped += 1
            self._warn("syslog TCP frame from %s dropped: %s", host, exc)


async def start_syslog_tcp(submit: Submit, host: str, port: int | None,
                           cfg: TcpConfig | None = None) -> TcpSyslogServer | None:
    """Start the listener; ``None`` when ``port`` is ``None`` (disabled) or the bind failed."""
    if port is None:
        return None
    server = TcpSyslogServer(submit, cfg)
    return server if await server.start(host, port) else None
