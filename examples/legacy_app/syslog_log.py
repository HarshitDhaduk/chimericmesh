"""Logging handler that ships records to a syslog collector as RFC 3164 UDP datagrams."""
from __future__ import annotations

import logging
import os
import re
import socket
import time

FACILITY_LOCAL0 = 16
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DEFAULT_ADDRESS = "127.0.0.1:5514"


def severity_for(levelno: int) -> int:
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    return 6


def clean_host(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", name)[:63] or "localhost"


def default_host() -> str:
    return clean_host(os.environ.get("LEGACY_APP_HOST") or socket.gethostname())


def parse_address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


class SyslogHandler(logging.Handler):
    def __init__(self, address: tuple[str, int] | None = None, host: str | None = None,
                 app: str = "proxy-worker") -> None:
        super().__init__()
        self.address = address or parse_address(os.environ.get("LEGACY_APP_SYSLOG", DEFAULT_ADDRESS))
        self.host = clean_host(host) if host else default_host()
        self.app = app
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def format_datagram(self, record: logging.LogRecord) -> bytes:
        pri = FACILITY_LOCAL0 * 8 + severity_for(record.levelno)
        now = time.localtime(record.created)
        stamp = f"{MONTHS[now.tm_mon - 1]} {now.tm_mday:2d} {time.strftime('%H:%M:%S', now)}"
        text = re.sub(r"[\r\n\x00]+", " ", record.getMessage())[:900]
        return f"<{pri}>{stamp} {self.host} {self.app}[{os.getpid()}]: {text}".encode("utf-8", "replace")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sock.sendto(self.format_datagram(record), self.address)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            super().close()
