"""WatchQueue (task 5.2): the long poll. Plain tests with ``asyncio.run`` and a small tick."""
from __future__ import annotations
import ast
import asyncio
import time
from pathlib import Path
import pytest
from agent_helpers import FakeState, make_incident, make_proposal
from chimericmesh.agent.errors import TooManyWaiters
from chimericmesh.agent.ports import AgentPorts
from chimericmesh.agent.watch import WatchQueue
SLACK = 0.5       # a loaded host (other pytest runs) delays the loop; a real defect costs seconds, not tenths
TIMEOUT = {"status": "timeout", "incidents": [], "approved_proposals": []}
class Registry:
    def __init__(self, *ids: str) -> None:
        self.ids = set(ids or ("svc",))
    def get(self, sid: str):
        return object() if sid in self.ids else None
def ports(state: FakeState, registry: Registry | None = None) -> AgentPorts:
    return AgentPorts(state=state, registry=registry or Registry(), approvals=None, protected_dirs=(),  # type: ignore[arg-type]
                      workspace_root=None)
def queue(state: FakeState | None = None, **kw) -> tuple[WatchQueue, FakeState]:
    state = state or FakeState()
    kw.setdefault("tick", 0.05)
    return WatchQueue(ports(state), **kw), state
def call(q: WatchQueue, timeout: float = 1, **kw):
    return q.next(timeout=timeout, service_id=kw.pop("service_id", None), exclude=kw.pop("exclude", frozenset()), **kw)
def test_work_present_returns_at_once():
    q, state = queue(FakeState(incidents=[make_incident("i1")]), tick=5.0)
    async def go():
        start = time.monotonic()
        out = await call(q, 10)
        return out, time.monotonic() - start
    out, took = asyncio.run(go())
    assert out["status"] == "work" and [i["id"] for i in out["incidents"]] == ["i1"] and took < 1.0
    assert q.waiting == 0
def test_incident_opened_later_is_returned_within_a_tick():
    q, state = queue(tick=0.05)
    async def go():
        async def opener():
            await asyncio.sleep(0.2)
            state._incidents.append(make_incident("i1"))
        task = asyncio.create_task(opener())
        start = time.monotonic()
        out = await call(q, 5)
        took = time.monotonic() - start
        await task
        return out, took
    out, took = asyncio.run(go())
    assert out["status"] == "work"
    assert 0.18 <= took <= 0.2 + 0.05 + SLACK, took
def test_no_work_returns_timeout_at_the_timeout():
    q, _ = queue(tick=0.05)
    async def go():
        start = time.monotonic()
        out = await call(q, 0.3)
        return out, time.monotonic() - start
    out, took = asyncio.run(go())
    assert out == TIMEOUT
    assert 0.28 <= took <= 0.3 + 0.15 + SLACK, took
    assert q.waiting == 0
def test_kick_wakes_a_waiter_at_once():
    q, state = queue(tick=10.0)         # a tick this long would hide a missing wake-up
    async def go():
        async def opener():
            await asyncio.sleep(0.1)
            state._incidents.append(make_incident("i1"))
            q.kick()
        task = asyncio.create_task(opener())
        start = time.monotonic()
        out = await call(q, 5)
        took = time.monotonic() - start
        await task
        return out, took
    out, took = asyncio.run(go())
    assert out["status"] == "work" and took < 2.0, took
def test_five_concurrent_waiters_give_four_held_and_one_429():
    q, _ = queue(tick=0.05)
    async def go():
        tasks = [asyncio.create_task(call(q, 0.4)) for _ in range(5)]
        done = await asyncio.gather(*tasks, return_exceptions=True)
        return done
    done = asyncio.run(go())
    errors = [d for d in done if isinstance(d, TooManyWaiters)]
    assert len(errors) == 1 and errors[0].retry_after == 5
    assert [d for d in done if isinstance(d, dict)] == [TIMEOUT] * 4
    assert q.waiting == 0
def test_waiting_counter_is_zero_after_return_cancel_and_close():
    q, state = queue(tick=0.05)
    async def go():
        await call(q, 0.1)
        assert q.waiting == 0
        task = asyncio.create_task(call(q, 30))
        await asyncio.sleep(0.15)
        assert q.waiting == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert q.waiting == 0
        held = [asyncio.create_task(call(q, 30)) for _ in range(3)]
        await asyncio.sleep(0.1)
        assert q.waiting == 3
        q.close()
        assert [await h for h in held] == [TIMEOUT] * 3
        assert q.waiting == 0
    asyncio.run(go())
def test_disconnect_frees_the_slot_within_a_tick_and_returns_timeout():
    q, _ = queue(tick=0.05)
    async def go():
        for _ in range(5):                      # five abandoned calls never reach the fifth-waiter 429
            gone = asyncio.Event()
            async def is_disconnected(flag=gone):
                return flag.is_set()
            task = asyncio.create_task(call(q, 30, is_disconnected=is_disconnected))
            await asyncio.sleep(0.12)
            assert q.waiting == 1
            gone.set()
            start = time.monotonic()
            out = await task
            assert out == TIMEOUT
            assert time.monotonic() - start < 0.05 + 0.05 + SLACK
            assert q.waiting == 0
    asyncio.run(go())
def test_disconnect_check_runs_once_per_tick_not_in_a_busy_loop():
    q, _ = queue(tick=0.05)
    calls = []
    async def is_disconnected():
        calls.append(time.monotonic())
        return False
    asyncio.run(call(q, 0.5, is_disconnected=is_disconnected))
    assert 5 <= len(calls) <= 14, len(calls)
def test_close_ends_every_held_waiter_at_once_and_a_later_call_times_out():
    q, _ = queue(tick=10.0)
    async def go():
        held = [asyncio.create_task(call(q, 30)) for _ in range(4)]
        await asyncio.sleep(0.1)
        start = time.monotonic()
        q.close()
        results = await asyncio.gather(*held)
        took = time.monotonic() - start
        assert results == [TIMEOUT] * 4 and took < 2.0
        assert await call(q, 30) == TIMEOUT
    asyncio.run(go())
def test_work_set_filters_apply():
    state = FakeState(incidents=[make_incident("i1"), make_incident("i2", service_id="other")],
                      proposals=[make_proposal("p1", "i9", status="approved", service_id="svc")])
    q = WatchQueue(ports(state, Registry("svc", "other")), tick=0.05)
    out = asyncio.run(call(q, 1, service_id="other", exclude=frozenset()))
    assert [i["id"] for i in out["incidents"]] == ["i2"] and out["approved_proposals"] == []
    out = asyncio.run(call(q, 1, exclude=frozenset({"i1", "i2", "p1"}), ))
    assert out == TIMEOUT or out["status"] == "timeout"
def test_state_read_failure_does_not_leak_the_slot():
    class Broken(FakeState):
        def incidents(self, **kw):
            raise RuntimeError("boom")
    q = WatchQueue(ports(Broken()), tick=0.05)
    with pytest.raises(RuntimeError):
        asyncio.run(call(q, 1))
    assert q.waiting == 0
def test_four_held_waiters_do_not_delay_ingestion():
    from chimericmesh.hub import Hub
    from agent_helpers import make_event
    hub = Hub()
    q = WatchQueue(AgentPorts(state=hub, registry=Registry(), approvals=None, protected_dirs=(),  # type: ignore[arg-type]
                              workspace_root=None), tick=0.05)
    async def go():
        held = [asyncio.create_task(call(q, 30)) for _ in range(4)]
        await asyncio.sleep(0.1)
        start = time.monotonic()
        for n in range(200):
            await hub.ingest(make_event(n + 1, None, message=f"line {n}"))
        took = time.monotonic() - start
        q.close()
        await asyncio.gather(*held)
        return took
    assert asyncio.run(go()) < 2.0
def test_module_has_no_threading_import_and_no_blocking_sleep():
    path = Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "agent" / "watch.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] in ("threading", "time") for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in ("threading", "time")
        if isinstance(node, ast.Attribute) and node.attr == "sleep":
            assert not (isinstance(node.value, ast.Name) and node.value.id == "time")
