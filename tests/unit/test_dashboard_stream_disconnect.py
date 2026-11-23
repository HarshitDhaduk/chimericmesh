"""A client that leaves frees its stream slot (dashboard task 3.2; R5.3, H4).

The first test drives the real create_app through a hand-written ASGI call (no socket): read the first body
message, deliver http.disconnect, and expect the slot back within 5 seconds. The second is the safety net:
a started lease whose generator stopped touching it is reclaimed by the next acquire.
"""
from __future__ import annotations

import asyncio
import time

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
from chimericmesh.api import create_app
from chimericmesh.hub import Hub


def scope(path: str) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8000")], "client": ("127.0.0.1", 4000), "server": ("127.0.0.1", 8000),
    }


def test_disconnect_frees_the_slot_through_the_real_app():
    app = create_app(Hub(), start_listeners=False, enforce_access=False)
    gate = app.state.stream_gate
    sent: list[dict] = []

    async def go():
        first_body = asyncio.Event()
        inbox = asyncio.Queue()
        await inbox.put({"type": "http.request", "body": b"", "more_body": False})

        async def receive():
            return await inbox.get()

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        task = asyncio.create_task(app(scope("/api/services/unassigned/stream"), receive, send))
        await asyncio.wait_for(first_body.wait(), 5)
        assert gate.active == 1
        await inbox.put({"type": "http.disconnect"})
        deadline = time.monotonic() + 5
        while gate.active and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        freed = gate.active == 0
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return freed

    freed = asyncio.run(go())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert freed, "the slot was not freed within 5 s of the disconnect (design O12)"
    app.state.store.close()


def test_a_stalled_started_lease_is_reclaimed_by_the_next_acquire():
    app = create_app(Hub(), start_listeners=False, enforce_access=False)
    gate = app.state.stream_gate
    leases = [gate.acquire(100.0) for _ in range(20)]
    assert all(leases) and gate.acquire(100.0) is None
    for lease in leases:
        lease.start(100.0)  # their generators then stop touching them
    assert gate.acquire(105.0) is None
    assert gate.acquire(100.0 + gate.stall_after + 1) is not None
    app.state.store.close()
