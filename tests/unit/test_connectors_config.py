"""Task 2.2: connector defaults and the TCP port rule."""
from __future__ import annotations

import dataclasses
import logging

import pytest

from chimericmesh.connectors.config import ConnectorConfig, TcpConfig, resolve_tcp_port

ENV = "CHIMERICMESH_SYSLOG_TCP_PORT"


def test_connector_defaults():
    c = ConnectorConfig()
    assert (c.poll_interval, c.catchup_interval) == (0.5, 0.1)
    assert (c.missing_retry, c.unreadable_retry, c.error_retry, c.not_regular_retry) == (5.0, 30.0, 5.0, 30.0)
    assert c.busy_polls == 5
    assert (c.flush_interval, c.reconcile_interval) == (5.0, 30.0)
    assert (c.max_read, c.max_line, c.head_size) == (1 << 20, 8192, 256)
    assert (c.rate, c.burst, c.drop_notice_interval) == (2000.0, 2000.0, 10.0)
    assert c.stop_grace == 2.0


def test_tcp_defaults():
    t = TcpConfig()
    assert (t.max_connections, t.idle_timeout, t.max_frame, t.read_size) == (64, 60.0, 8192, 65536)


def test_configs_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ConnectorConfig().rate = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        TcpConfig().max_frame = 1  # type: ignore[misc]


def test_port_default_disabled_and_valid(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_tcp_port({}) == 5514
        assert resolve_tcp_port({ENV: "0"}) is None
        assert resolve_tcp_port({ENV: "6514"}) == 6514
        assert resolve_tcp_port({ENV: "65535"}) == 65535
    assert caplog.records == []


@pytest.mark.parametrize("value", ["65536", "-1", "abc", "", "5514 ", " 5514", "1e3", "+80", "６５１４"])
def test_invalid_port_warns_once_and_uses_default(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_tcp_port({ENV: value}) == 5514
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and ENV in warnings[0].getMessage()


def test_reads_only_the_given_mapping(monkeypatch):
    monkeypatch.setenv(ENV, "7000")
    assert resolve_tcp_port({}) == 5514
