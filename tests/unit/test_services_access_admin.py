"""Admin check and the wrong-token limiter (R6.2 to R6.4, R6.7)."""
from __future__ import annotations
import asyncio
import hmac
import logging

import pytest

from chimericmesh.services import access as access_mod
from chimericmesh.services.access import AccessControl, AdminResult
from chimericmesh.services.token import TokenStore

HOSTS = frozenset({"localhost"})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ac(svc_mem_store, clock):
    tokens = TokenStore.open(None, in_memory=True, redact=svc_mem_store.add_redaction)
    return AccessControl(enforce=True, tokens=tokens, allowed_hosts=HOSTS, audit=svc_mem_store.audit, now=clock)


def run(coro):
    return asyncio.run(coro)


def rows(store):
    return run(store.audit.read(limit=500))


def check(ac, presented, client="1.2.3.4", op="POST /api/services"):
    return run(ac.check_admin(presented, client=client, operation=op))


def test_disabled_allows_everything_and_writes_nothing(svc_mem_store):
    off = AccessControl.disabled()
    assert off.enforce is False
    assert run(off.check_admin("junk", client="x", operation="op")).result is AdminResult.OK
    assert run(off.check_admin(None, client="x", operation="op")).result is AdminResult.OK
    assert off.host_allowed("evil.example") is True and off.verify_token(None) is True


def test_right_token_ok_and_writes_nothing(ac, svc_mem_store):
    assert check(ac, ac.tokens.current()).result is AdminResult.OK
    assert rows(svc_mem_store) == []


@pytest.mark.parametrize("bad", [None, "", "wrong", "x" * 5000, "ключ"])
def test_wrong_token_unauthorized_with_one_audit_row(ac, svc_mem_store, bad):
    out = check(ac, bad)
    assert out.result is AdminResult.UNAUTHORIZED
    r = rows(svc_mem_store)
    assert len(r) == 1
    assert r[0]["actor"] == "client:1.2.3.4" and r[0]["action"] == "auth_failed"
    assert r[0]["subject"] == "POST /api/services" and r[0]["detail"] == ""
    if bad:
        assert bad not in str(r)


def test_eleventh_wrong_token_is_rate_limited_without_row(ac, svc_mem_store, clock):
    for i in range(10):
        assert check(ac, f"w{i}").result is AdminResult.UNAUTHORIZED
        clock.t += 1
    out = check(ac, "w10")
    assert out.result is AdminResult.RATE_LIMITED and 0 < out.retry_after <= 60
    assert len(rows(svc_mem_store)) == 10
    for _ in range(5):
        assert check(ac, "again").result is AdminResult.RATE_LIMITED
    assert len(rows(svc_mem_store)) == 10                              # exactly 10 per address per minute (O3)
    assert check(ac, ac.tokens.current()).result is AdminResult.OK     # the right token still passes
    assert check(ac, "w", client="9.9.9.9").result is AdminResult.UNAUTHORIZED   # another address unaffected
    clock.t += 61
    assert check(ac, "w").result is AdminResult.UNAUTHORIZED           # block ended


def test_tracked_addresses_are_capped_at_512(ac):
    for i in range(600):
        check(ac, "w", client=f"10.0.{i // 250}.{i % 250}")
    assert len(ac._limiter._entries) <= 512


def test_audit_failure_still_unauthorized(svc_mem_store, clock):
    class Broken:
        async def append(self, *a, **k):
            raise RuntimeError("store down")

    tokens = TokenStore.open(None, in_memory=True)
    broken = AccessControl(enforce=True, tokens=tokens, allowed_hosts=HOSTS, audit=Broken(), now=clock)
    assert check(broken, "wrong").result is AdminResult.UNAUTHORIZED


def test_comparison_uses_compare_digest(ac, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(access_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert ac.verify_token(ac.tokens.current()) is True
    assert ac.verify_pass("nope") is False
    assert len(calls) == 2


@pytest.mark.parametrize("value", [None, "", "é" * 50, "\ud800", "x" * 10_000])
def test_verify_accepts_odd_input_without_raising(ac, value):
    assert ac.verify_token(value) is False and ac.verify_pass(value) is False


def test_token_never_in_audit_or_logs(ac, svc_mem_store, caplog):
    secret = ac.tokens.current()
    with caplog.at_level(logging.DEBUG):
        check(ac, secret + "x")
        check(ac, secret[:-1])
        check(ac, secret)
    assert secret not in str(rows(svc_mem_store))
    assert secret not in caplog.text
    assert secret not in repr(ac) and secret not in repr(ac.tokens)
