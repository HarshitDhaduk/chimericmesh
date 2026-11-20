"""Defaults and limits of the connectors. Timings are constructor arguments so tests can shrink them."""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

log = logging.getLogger("chimericmesh.connectors")

DEFAULT_TCP_PORT = 5514
TCP_PORT_ENV = "CHIMERICMESH_SYSLOG_TCP_PORT"


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    poll_interval: float = 0.5
    catchup_interval: float = 0.1
    missing_retry: float = 5.0
    unreadable_retry: float = 30.0
    error_retry: float = 5.0           # an unexpected exception in the loop or the executor
    not_regular_retry: float = 30.0    # status error "not a regular file" (directory, device, FIFO, socket)
    busy_polls: int = 5                # consecutive sharing/lock violations before the status becomes unreadable
    flush_interval: float = 5.0
    reconcile_interval: float = 30.0
    max_read: int = 1 << 20
    max_line: int = 8192
    head_size: int = 256
    rate: float = 2000.0
    burst: float = 2000.0
    drop_notice_interval: float = 10.0
    stop_grace: float = 2.0


@dataclass(frozen=True, slots=True)
class TcpConfig:
    max_connections: int = 64
    idle_timeout: float = 60.0
    max_frame: int = 8192
    read_size: int = 65536


def resolve_tcp_port(environ: Mapping[str, str]) -> int | None:
    """The syslog TCP port: 5514 by default, ``None`` when the variable is ``0`` (disabled)."""
    value = environ.get(TCP_PORT_ENV)
    if value is None:
        return DEFAULT_TCP_PORT
    if re.fullmatch(r"[0-9]{1,5}", value):
        port = int(value)
        if port == 0:
            return None
        if port <= 65535:
            return port
    log.warning("%s=%r is not a port from 0 to 65535; using %d", TCP_PORT_ENV, value[:40], DEFAULT_TCP_PORT)
    return DEFAULT_TCP_PORT
