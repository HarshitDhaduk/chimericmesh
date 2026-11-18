"""R7.2 benchmark: 5000 events at 1000 per second from 200 signatures, with incident-open commits.

Skipped when CHIMERICMESH_SKIP_BENCH=1. Thresholds are the requirement's (tolerant by design).
"""
import asyncio
import os
import time

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("CHIMERICMESH_SKIP_BENCH") == "1", reason="CHIMERICMESH_SKIP_BENCH=1"),
]

TOTAL, RATE, SIGNATURES = 5000, 1000, 200


def test_burst_is_persisted_without_stalling_the_loop(tmp_path):
    store = Store.open(tmp_path / "bench.db", registry=core_registry())

    async def go():
        hub = Hub(store=store)
        stalls: list[float] = []
        done = asyncio.Event()

        async def watchdog():
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                stalls.append(now - last - 0.005)
                last = now

        dog = asyncio.ensure_future(watchdog())
        start = time.perf_counter()
        for n in range(TOTAL):
            sig = n % SIGNATURES
            await hub.ingest(Event("syslog", f"host{sig}", "app", 3, f"failure kind {chr(65 + sig % 26)}{sig // 26} "
                                   f"no space left", "", 1.0))
            target = start + (n + 1) / RATE
            # Always yield, as the listeners do (each datagram or line is its own loop callback).
            await asyncio.sleep(max(0.0, target - time.perf_counter()))
        last_event = time.perf_counter()
        await asyncio.wait_for(store.events.barrier(), timeout=5.0)
        flushed_in = time.perf_counter() - last_event
        done.set()
        await dog
        persisted = await store.read(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0])
        incidents = await store.read(lambda tx: tx.one("SELECT COUNT(*) FROM incidents")[0])
        await hub.aclose()
        return flushed_in, max(stalls), persisted, store.health().dropped_events, incidents

    flushed_in, stall, persisted, dropped, incidents = asyncio.run(go())
    store.close()
    assert flushed_in < 5.0
    assert persisted == TOTAL or persisted + dropped == TOTAL
    assert stall < 0.100, f"max loop stall {stall * 1000:.1f} ms"
    assert incidents == SIGNATURES  # every incident-open commit happened during the burst
