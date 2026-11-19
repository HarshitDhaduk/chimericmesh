"""Host allow-list, parse_allowed_hosts and bind check (R7.2 to R7.4, R7.6)."""
from __future__ import annotations
import logging

import pytest

from chimericmesh.services.access import DEFAULT_ALLOWED_HOSTS, host_allowed, is_loopback, parse_allowed_hosts

ALLOWED = frozenset({"127.0.0.1", "localhost", "::1"})


@pytest.mark.parametrize("header", ["127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:1", "[::1]", "[::1]:8000"])
def test_allowed(header):
    assert host_allowed(header, ALLOWED) is True


@pytest.mark.parametrize("header", [
    "evil.example", "127.0.0.1.evil.example", "evil.example@127.0.0.1", "127.0.0.1@evil.example",
    "127.0.0.1 evil", "127.0.0.1:80:80", "localhost.", "", None, "a" * 301, "localhost\n", "local\x00host",
    "localhost\x07", "local\\host", "localhost:", "localhost:123456"])
def test_rejected(header):
    assert host_allowed(header, ALLOWED) is False


def test_parse_default():
    assert parse_allowed_hosts(None) == DEFAULT_ALLOWED_HOSTS == ALLOWED


def test_parse_normalizes():
    assert parse_allowed_hosts("A.example, [::1]:80") == frozenset({"a.example", "::1"})


def test_invalid_entry_skipped_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="chimericmesh.services"):
        out = parse_allowed_hosts("ok.example, bad host, evil@x")
    assert out == frozenset({"ok.example"})
    assert len([r for r in caplog.records if "CHIMERICMESH_ALLOWED_HOSTS" in r.getMessage()]) == 1


def test_all_invalid_falls_back_to_default():
    assert parse_allowed_hosts("a b, c@d") == DEFAULT_ALLOWED_HOSTS
    assert parse_allowed_hosts("") == DEFAULT_ALLOWED_HOSTS
    assert parse_allowed_hosts(" , ,") == DEFAULT_ALLOWED_HOSTS


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.5.5.5", "::1", "localhost", "LOCALHOST"])
def test_loopback(bind):
    assert is_loopback(bind) is True


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "", "192.168.1.5", "myhost"])
def test_not_loopback(bind):
    assert is_loopback(bind) is False
