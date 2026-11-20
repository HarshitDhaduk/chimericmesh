"""Task 3.3: the TCP syslog server on real sockets (port 0) with a recording ``submit``."""
from __future__ import annotations

import asyncio
import logging

from chimericmesh.connectors.config import TcpConfig
from chimericmesh.connectors.syslog_tcp import TcpSyslogServer
from chimericmesh.events import Event


class Rec:
    def __init__(self, fail_on: int | None = None) -> None:
        self.events: list[Event] = []
        self.calls = 0
        self.fail_on = fail_on

    async def __call__(self, event: Event, log_source_id: str | None = None) -> Event:
        self.calls += 1
        if self.fail_on == self.calls:
            raise RuntimeError("ingest failed")
        self.events.append(event)
        return event


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def serve(rec, cfg: TcpConfig | None = None) -> TcpSyslogServer:
    server = TcpSyslogServer(rec, cfg)
    assert await server.start("127.0.0.1", 0)
    assert server.port
    return server


async def connect(server: TcpSyslogServer):
    return await asyncio.open_connection("127.0.0.1", server.port)


async def is_closed(reader: asyncio.StreamReader, timeout: float = 3.0) -> bool:
    try:
        return await asyncio.wait_for(reader.read(1), timeout) == b""
    except (ConnectionError, OSError):
        return True


def octet(text: str) -> bytes:
    body = text.encode()
    return b"%d " % len(body) + body


def run(coro):
    return asyncio.run(coro)


def test_octet_and_lf_events_are_parsed_with_pri():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(octet("<34>Nov 18 10:00:00 web01 sshd[9]: octet message"))
            await w.drain()
            _, w2 = await connect(server)
            w2.write(b"<11>Nov 18 10:00:00 web02 app: lf message\n")
            await w2.drain()
            await until(lambda: len(rec.events) == 2)
            by_msg = {e.message: e for e in rec.events}
            a, b = by_msg["octet message"], by_msg["lf message"]
            assert a.source == "syslog" and a.severity == 2 and a.host == "web01" and a.app == "sshd"
            assert b.source == "syslog" and b.severity == 3 and b.host == "web02"
            assert server.stats.frames == 2
            w.close()
            w2.close()
        finally:
            await server.aclose()

    run(main())


def test_peer_ip_is_host_when_message_has_none():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(b"<11>just some text\n")
            await w.drain()
            await until(lambda: len(rec.events) == 1)
            assert rec.events[0].host == "127.0.0.1"
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_invalid_utf8_is_replaced():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(b"<11>Nov 18 10:00:00 h app: bad \xff\xfe bytes\n")
            await w.drain()
            await until(lambda: len(rec.events) == 1)
            assert "\ufffd" in rec.events[0].message
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_100kb_octet_frame_in_octet_mode_closes_only_that_connection():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            r1, w1 = await connect(server)
            w1.write(octet("<11>Nov 18 10:00:00 h app: first"))
            await w1.drain()
            other_r, other_w = await connect(server)
            other_w.write(octet("<11>Nov 18 10:00:00 h app: other one"))
            await other_w.drain()
            await until(lambda: len(rec.events) == 2)
            w1.write(b"102400 " + b"a" * 102400)
            await w1.drain()
            assert await is_closed(r1)
            assert server.stats.dropped == 1 and server.stats.framing_errors == 1
            other_w.write(octet("<11>Nov 18 10:00:00 h app: still works"))
            await other_w.drain()
            await until(lambda: len(rec.events) == 3)
            assert rec.events[-1].message == "still works"
            other_w.close()
        finally:
            await server.aclose()

    run(main())


def test_first_bytes_9999_close_the_connection():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            r, w = await connect(server)
            w.write(b"9999 hello")
            await w.drain()
            assert await is_closed(r)
            assert server.stats.dropped == 1 and server.stats.framing_errors == 1
            assert rec.events == []
        finally:
            await server.aclose()

    run(main())


def test_fresh_connection_102400_frame_is_lf_and_truncated():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            r, w = await connect(server)
            w.write(b"102400 " + b"a" * 102400 + b"\n")
            w.write(b"<11>Nov 18 10:00:00 h app: after\n")
            await w.drain()
            await until(lambda: len(rec.events) == 2)
            assert len(rec.events[0].message) <= 8192 and rec.events[0].message.startswith("102400 aaa")
            assert len(rec.events[0].message) == 8192
            assert rec.events[1].message == "after"
            assert server.stats.dropped == 0 and server.stats.framing_errors == 0
            assert not r.at_eof()
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_iso_timestamp_first_line_is_lf_and_stays_open():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            r, w = await connect(server)
            w.write(b"2026-11-18T10:00:00 host app: x\n")
            await w.drain()
            await until(lambda: len(rec.events) == 1)
            assert server.stats.dropped == 0 and server.stats.framing_errors == 0
            assert not r.at_eof()
            w.write(b"second\n")
            await w.drain()
            await until(lambda: len(rec.events) == 2)
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_65th_connection_is_refused():
    async def main():
        rec = Rec()
        server = await serve(rec)
        clients = []
        try:
            for _ in range(64):
                clients.append(await connect(server))
            await until(lambda: server.stats.active == 64)
            r65, w65 = await connect(server)
            assert await is_closed(r65)
            assert server.stats.refused == 1
            assert server.stats.active == 64
            clients[0][1].write(b"<11>Nov 18 10:00:00 h app: alive\n")
            await clients[0][1].drain()
            await until(lambda: len(rec.events) == 1)
            w65.close()
        finally:
            for _, w in clients:
                w.close()
            await server.aclose()

    run(main())


def test_idle_connection_is_closed():
    async def main():
        rec = Rec()
        server = await serve(rec, TcpConfig(idle_timeout=0.2))
        try:
            r, w = await connect(server)
            assert await is_closed(r, 3.0)
            await until(lambda: server.stats.idle_closed == 1)
            assert server.stats.active == 0
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_submit_exception_drops_one_frame_and_continues(caplog):
    async def main():
        rec = Rec(fail_on=1)
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(b"<11>Nov 18 10:00:00 h app: one\n<11>Nov 18 10:00:00 h app: two\n")
            await w.drain()
            await until(lambda: len(rec.events) == 1)
            assert rec.events[0].message == "two"
            assert server.stats.dropped == 1 and server.stats.frames == 1
            assert server.stats.framing_errors == 0
            w.close()
        finally:
            await server.aclose()

    with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
        run(main())


def test_multiline_octet_frame_is_one_event():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(octet("<11>Nov 18 10:00:00 h app: line one\nline two\r\nline three"))
            await w.drain()
            await until(lambda: len(rec.events) == 1)
            await asyncio.sleep(0.1)
            assert len(rec.events) == 1
            assert "\n" not in rec.events[0].message and "line three" in rec.events[0].message
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_server_never_writes_to_the_client():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            r, w = await connect(server)
            w.write(octet("<11>Nov 18 10:00:00 h app: hello") + octet("<11>Nov 18 10:00:00 h app: x"))
            await w.drain()
            await until(lambda: len(rec.events) >= 1)
            try:
                data = await asyncio.wait_for(r.read(10), 0.3)
            except asyncio.TimeoutError:
                data = None
            assert data is None
            w.close()
        finally:
            await server.aclose()

    run(main())


def test_connection_closed_mid_frame_leaves_no_task():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            before = {t for t in asyncio.all_tasks()}
            _, w = await connect(server)
            w.write(b"100 partial")
            await w.drain()
            await until(lambda: server.stats.active == 1)
            w.close()
            await until(lambda: server.stats.active == 0)
            await asyncio.sleep(0.1)
            leftover = {t for t in asyncio.all_tasks() if not t.done()} - before
            leftover = {t for t in leftover if "_handle" in repr(t.get_coro())}
            assert not leftover and not server._tasks  # noqa: SLF001
            assert rec.events == []
        finally:
            await server.aclose()

    run(main())


def test_unterminated_lf_line_at_eof_is_delivered():
    async def main():
        rec = Rec()
        server = await serve(rec)
        try:
            _, w = await connect(server)
            w.write(b"<11>Nov 18 10:00:00 h app: last words")
            await w.drain()
            w.close()
            await until(lambda: len(rec.events) == 1)
        finally:
            await server.aclose()

    run(main())
