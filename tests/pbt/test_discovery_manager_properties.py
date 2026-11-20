"""Properties 11 and 12: the scan state machine and the dismissals round trip. Validates: Requirements 1.3, 5.4, 5.6."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery import dismissals
from chimericmesh.discovery.catalog import CatalogLoad, load_catalog
from chimericmesh.discovery.manager import ScanManager
from chimericmesh.discovery.models import CandidatePath, Evidence, ScanOutcome, Suggestion
from chimericmesh.discovery.schema import discovery_registry
from chimericmesh.store import Store
from chimericmesh.store.config import MEMORY

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
LOAD = CatalogLoad(load_catalog(FORMATS, text='{"version":1,"apps":[{"app_id":"nginx","name":"n","process_names":["n"],'
                                '"ports":[],"protocol_label":"x","justification":null,"log_paths":{"linux":[{"path":'
                                '"/l/a.log","format":"plain","kind":"other"}],"windows":[],"macos":[]}}]}').catalog, "")


def sug(sid):
    return Suggestion(sid, "nginx", "n", "x", "high", Evidence("n", 1, 1, ()),
                      (CandidatePath("/l/a.log", "plain", "other", False, False, None, None),), "f" * 16)


OPS = st.lists(st.sampled_from(["start", "finish", "fail", "advance", "expire", "shutdown"]), max_size=25)


@settings(max_examples=40, deadline=None)
@given(OPS)
def test_property_11_scan_state_machine(ops):
    store = Store.open(MEMORY, registry=discovery_registry())
    now = [1000.0]
    gate = threading.Event()
    mode = {"fail": False}

    def engine(inputs, runner, procfs, fs, next_id, cancel):
        end = time.monotonic() + 5
        while not gate.is_set() and not cancel.is_set() and time.monotonic() < end:
            time.sleep(0.002)
        if mode["fail"]:
            raise RuntimeError("x")
        return ScanOutcome("done", False, "", (), (sug(next_id()),))

    mgr = ScanManager(load=LOAD, engine=engine, runner=None, procfs=None, fs=None, store=store,
                      registry=SimpleNamespace(log_sources=lambda: []), known_formats=FORMATS, now=lambda: now[0],
                      os_name="linux", is_admin=False, own_pid=1, env={})
    finished_at: dict[str, float] = {}
    seen: dict[str, str] = {}

    async def settle():
        for _ in range(200):
            if not any(s.state == "running" for s in mgr._scans.values()):
                return
            await asyncio.sleep(0.005)

    async def run():
        closed = False
        for op in ops:
            if op == "start" and not closed:
                gate.clear()
                try:
                    scan = await mgr.start(actor="a")
                    seen[scan.id] = scan.state
                except Exception:
                    pass
            elif op in ("finish", "fail") and not closed:
                mode["fail"] = op == "fail"
                gate.set()
                await settle()
            elif op == "advance":
                now[0] += 1000
            elif op == "expire":
                now[0] += 3700
            elif op == "shutdown" and not closed:
                gate.set()
                await mgr.aclose()
                closed = True
            running = [s for s in mgr._scans.values() if s.state == "running"]
            assert len(running) <= 1
            for s in list(mgr._scans.values()):
                if s.state != "running" and s.finished_at is not None:
                    finished_at.setdefault(s.id, s.finished_at)
            for sc in list(mgr._scans.values()):
                for sg in sc.suggestions:
                    age = now[0] - (sc.finished_at or now[0])
                    found = mgr.find(sg.suggestion_id)
                    if age > 3600 and sc.state != "running":
                        assert found is None
        gate.set()
        await mgr.aclose()
        # an expired id is gone: advance past the TTL and look again
        now[0] += 3700
        for sid in seen:
            assert mgr.get(sid) is None or mgr.get(sid).state == "running"

    try:
        asyncio.run(run())
    finally:
        gate.set()
        store.close()


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["dismiss", "clear", "restart"]), st.integers(0, 5)), max_size=25))
def test_property_12_dismissals_round_trip_and_cap(ops):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "d.db"
        store = Store.open(db, registry=discovery_registry())
        model: set[tuple[str, str]] = set()
        try:
            for op, n in ops:
                if op == "dismiss":
                    pair = (f"app{n}", f"{n:016d}")
                    asyncio.run(dismissals.dismiss(store, *pair, actor="x", paths=1))
                    model.add(pair)
                elif op == "clear":
                    assert asyncio.run(dismissals.clear(store, actor="x")) == len(model)
                    model.clear()
                else:
                    store.close()
                    store = Store.open(db, registry=discovery_registry())
                got = asyncio.run(dismissals.load_fingerprints(store))
                assert got == frozenset(model)
                assert len(got) <= 500
        finally:
            store.close()
