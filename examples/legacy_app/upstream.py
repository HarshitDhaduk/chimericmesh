"""Proxy worker that health-checks its upstream service."""
from __future__ import annotations

import logging
import socket
import time
from typing import Callable

log = logging.getLogger("proxy-worker")


class UpstreamClient:
    """Pings the upstream over TCP and expects a reply within ``timeout`` seconds."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 19099,
        timeout: float = 0.2,
        interval: float = 1.0,
        connect: Callable[..., socket.socket] = socket.create_connection,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host, self.port = host, port
        self.timeout, self.interval = timeout, interval
        self._connect, self._sleep, self._clock = connect, sleep, clock
        self.last_ok: float | None = None

    def check(self) -> None:
        sock = self._connect((self.host, self.port), timeout=self.timeout)
        try:
            sock.sendall(b"PING\n")
            sock.recv(64)
        except socket.timeout:
            log.error("upstream timed out connecting to %s:%d", self.host, self.port)
            raise
        finally:
            sock.close()
        self.last_ok = self._clock()

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            try:
                self.check()
            except OSError:
                continue
            self._sleep(self.interval)
