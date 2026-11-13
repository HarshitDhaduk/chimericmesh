"""Sample reverse-proxy upstream check (its log line matches the demo storm)."""

import logging
import socket

log = logging.getLogger("web02.proxy")


def connect_upstream(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        log.error("upstream timed out connecting to %s:%d", host, port)
        raise
