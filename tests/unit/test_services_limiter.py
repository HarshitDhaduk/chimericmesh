"""Reduce-operations limiter (R6.5)."""
from __future__ import annotations
import asyncio

import pytest

from chimericmesh.services.limiter import ReduceAuditLimiter, reduce_key


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeAudit:
    def __init__(self):
        self.rows = []
        self.fail = False

    async def append(self, actor, action, subject, detail="", **kw):
        if self.fail:
            raise RuntimeError("down")
        self.rows.append((actor, action, subject, detail, kw))
        return len(self.rows)


async def call(lim, key="rest:1", n=1):
    return [await lim.record(key, actor="a", action="rule_paused", subject="r1") for _ in range(n)]


def test_first_ten_get_rows_then_suppressed_and_flushed():
    async def go():
        clock, audit = Clock(), FakeAudit()
        lim = ReduceAuditLimiter(audit, clock)
        results = await call(lim, n=12)
        assert results == [True] * 10 + [False] * 2
        assert len(audit.rows) == 10 and all(r[1] == "rule_paused" for r in audit.rows)
        await lim.flush_due()
        assert len(audit.rows) == 10                       # window not over yet
        clock.t += 61
        await lim.flush_due()
        assert len(audit.rows) == 11
        assert audit.rows[-1][1] == "reduce_ops_suppressed" and audit.rows[-1][2] == "rest:1" and audit.rows[-1][3] == "count=2"
        await lim.flush_due()
        assert len(audit.rows) == 11                       # flushed once
        assert await call(lim) == [True]                   # a new window
        await lim.aclose()
    asyncio.run(go())


def test_aclose_flushes_open_window():
    async def go():
        audit = FakeAudit()
        lim = ReduceAuditLimiter(audit, Clock())
        await call(lim, n=13)
        await lim.aclose()
        assert audit.rows[-1][3] == "count=3"
    asyncio.run(go())


def test_keys_are_independent():
    async def go():
        lim = ReduceAuditLimiter(FakeAudit(), Clock())
        await call(lim, "a", 11)
        assert await call(lim, "b") == [True]
        await lim.aclose()
    asyncio.run(go())


def test_table_capped_and_evicted_pending_flushed_first():
    async def go():
        audit = FakeAudit()
        lim = ReduceAuditLimiter(audit, Clock(), max_keys=256)
        await call(lim, "first", 12)                       # 2 pending
        for i in range(300):
            await call(lim, f"k{i}")
        assert len(lim._windows) <= 256
        flushed = [r for r in audit.rows if r[1] == "reduce_ops_suppressed" and r[2] == "first"]
        assert len(flushed) == 1 and flushed[0][3] == "count=2"
        await lim.aclose()
    asyncio.run(go())


def test_audit_failure_changes_no_return_value(caplog):
    async def go():
        audit = FakeAudit()
        audit.fail = True
        lim = ReduceAuditLimiter(audit, Clock())
        assert await call(lim, n=12) == [True] * 10 + [False] * 2
        await lim.aclose()
    asyncio.run(go())
    assert "reduce" in caplog.text.lower()


def test_reduce_key_formats():
    assert reduce_key("rest", "127.0.0.1") == "127.0.0.1"
    assert reduce_key("irc", "alice") == "alice"
    assert reduce_key("terminal", "127.0.0.1") == "dashboard-terminal:127.0.0.1"
    with pytest.raises(ValueError):
        reduce_key("smoke", "x")


def test_scheduled_call_later_runs_flush_due():
    async def go():
        audit = FakeAudit()
        lim = ReduceAuditLimiter(audit, per_minute=1, window=0.05)     # real clock
        assert await call(lim, n=3) == [True, False, False]
        await asyncio.sleep(0.4)
        assert audit.rows[-1][1] == "reduce_ops_suppressed" and audit.rows[-1][3] == "count=2"
        await lim.aclose()
    asyncio.run(go())


def test_timer_firing_early_rearms_until_window_is_due():
    async def go():
        import time as _t
        audit = FakeAudit()
        lag = 0.03                                  # wall clock behind the loop timer
        lim = ReduceAuditLimiter(audit, lambda: _t.time() - lag, per_minute=1, window=0.1)
        assert await call(lim, n=3) == [True, False, False]
        await asyncio.sleep(0.5)
        assert audit.rows[-1][1] == "reduce_ops_suppressed" and audit.rows[-1][3] == "count=2"
        assert not lim._windows
        await lim.aclose()
    asyncio.run(go())

