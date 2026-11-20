"""Task 3.4: TCP listener lifecycle (disabled, port in use, shutdown)."""
from __future__ import annotations

import asyncio
import logging
import socket

from chimericmesh.connectors.syslog_tcp import TcpSyslogServer, start_syslog_tcp
from chimericmesh.events import Event


async def noop(event: Event, log_source_id: str | None = None) -> Event:
    return event


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_disabled_port_binds_nothing():
    async def main():
        assert await start_syslog_tcp(noop, "127.0.0.1", None) is None

    asyncio.run(main())


def test_port_in_use_returns_none_with_one_warning(caplog):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]

    async def main():
        server = TcpSyslogServer(noop)
        assert await server.start("127.0.0.1", port) is False
        assert server.port is None
        assert await start_syslog_tcp(noop, "127.0.0.1", port) is None

    try:
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            asyncio.run(main())
    finally:
        blocker.close()


def test_one_warning_for_one_failed_start(caplog):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]

    async def main():
        assert await start_syslog_tcp(noop, "127.0.0.1", port) is None

    try:
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            asyncio.run(main())
    finally:
        blocker.close()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert str(port) in caplog.records[-1].getMessage()


def test_udp_and_tcp_share_a_port_number():
    port = free_port()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", port))

    async def main():
        server = await start_syslog_tcp(noop, "127.0.0.1", port)
        assert server is not None and server.port == port
        await server.aclose()

    try:
        asyncio.run(main())
    finally:
        udp.close()


def test_aclose_with_idle_clients_and_a_client_that_never_reads():
    async def main():
        server = await start_syslog_tcp(noop, "127.0.0.1", free_port())
        assert server is not None
        clients = [await asyncio.open_connection("127.0.0.1", server.port) for _ in range(10)]
        for _, w in clients[:5]:
            w.write(b"<11>hello\n" * 200)
        for _ in range(100):
            if server.stats.active == 10:
                break
            await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await server.aclose(timeout=1.0)
        assert loop.time() - t0 < 1.5
        assert server.stats.active == 0
        await server.aclose()  # twice is harmless
        for r, _ in clients:
            assert await asyncio.wait_for(r.read(1), 2.0) == b""

    asyncio.run(main())


def test_default_bind_is_the_given_address_not_all_interfaces():
    async def main():
        server = await start_syslog_tcp(noop, "127.0.0.1", free_port())
        assert server is not None
        names = [s.getsockname()[0] for s in server._server.sockets]  # noqa: SLF001
        assert names == ["127.0.0.1"]
        await server.aclose()

    asyncio.run(main())


def test_port_can_be_bound_again_after_aclose():
    port = free_port()

    async def main():
        server = await start_syslog_tcp(noop, "127.0.0.1", port)
        assert server is not None
        await server.aclose()

    asyncio.run(main())
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    finally:
        s.close()
