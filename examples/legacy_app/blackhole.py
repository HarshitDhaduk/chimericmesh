"""A TCP listener that accepts connections and never answers them."""
from __future__ import annotations

import socket
import socketserver
import threading

DEFAULT_PORT = 19099


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(0.25)
        while not self.server.closing.is_set():  # type: ignore[attr-defined]
            try:
                if not self.request.recv(4096):
                    return
            except socket.timeout:
                continue
            except OSError:
                return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.closing = threading.Event()
        super().__init__(address, _Handler)


class Blackhole:
    """Accepts TCP connections on 127.0.0.1 and holds them open without replying."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self._server = _Server(("127.0.0.1", port))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "Blackhole":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.closing.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
