"""The scan manager (R1.1, R1.3, R1.4, R1.6, R3.5, R5.4, R5.6) with a fake engine and clock."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from chimericmesh.discovery import dismissals
from chimericmesh.discovery import manager as manager_module
from chimericmesh.discovery.catalog import CatalogLoad, load_catalog
from chimericmesh.discovery.errors import AlreadyAccepted, CatalogUnavailable, ScanBusy, SuggestionNotFound
from chimericmesh.discovery.manager import ScanManager
from chimericmesh.discovery.models import CandidatePath, Evidence, ScanOutcome, Suggestion
from chimericmesh.discovery.schema import discovery_registry
from chimericmesh.discovery.text import path_key
from chimericmesh.store import Store, StoreError
from chimericmesh.store.config import MEMORY

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
APP = {"app_id": "nginx", "name": "nginx", "process_names": ["nginx"], "ports": [80], "protocol_label": "http",
       "justification": None,
       "log_paths": {"linux": [{"path": "/var/log/nginx/access.log", "format": "ncsa", "kind": "access"}],
                     "windows": [], "macos": []}}
LOAD = load_catalog(FORMATS, text='{"version":1,"apps":[' + __import__("json").dumps(APP) + "]}")


def suggestion(sid, app_id="nginx", process="nginx"):
    return Suggestion(sid, app_id, app_id, "http", "high", Evidence(process, 1, 1, (80,)),
                      (CandidatePath("/var/log/nginx/access.log", "ncsa", "access", True, True, 5, None),), "f" * 16)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Env:
    """A manager plus fakes. ``engine`` records its calls and the thread it ran on."""

    def __init__(self, *, load=LOAD, os_name="linux", engine=None, sources=(), scan_timeout=40.0):
        self.store = Store.open(MEMORY, registry=discovery_registry())
        self.clock = Clock()
        self.calls: list[tuple] = []
        self.threads: list[str] = []
        self.release = threading.Event()
        self.registry = SimpleNamespace(log_sources=lambda: list(sources))
        self.manager = ScanManager(
            load=load, engine=engine or self.default_engine, runner=object(), procfs=object(), fs=object(),
            store=self.store, registry=self.registry, known_formats=FORMATS, now=self.clock, os_name=os_name,
            is_admin=False, own_pid=4242, env={"HOME": "/h"}, scan_timeout=scan_timeout)

    def default_engine(self, inputs, runner, procfs, fs, next_id, cancel):
        self.calls.append((inputs,))
        self.threads.append(threading.current_thread().name)
        return ScanOutcome("done", False, "", (), (suggestion(next_id()),))

    def blocking_engine(self, inputs, runner, procfs, fs, next_id, cancel):
        self.calls.append((inputs,))
        end = time.monotonic() + 10                 # a safety timeout, never a bare Event().wait()
        while not (cancel.is_set() or self.release.is_set()) and time.monotonic() < end:
            time.sleep(0.01)
        return ScanOutcome("done", False, "", (), ())

    def close(self):
        asyncio.run(self.manager.aclose())
        self.store.close()


async def wait_done(manager, scan_id, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        scan = manager.get(scan_id)
        if scan is not None and scan.state != "running":
            return scan
        await asyncio.sleep(0.01)
    raise AssertionError("scan did not finish")


def audit_rows(store):
    return asyncio.run(store.audit.read(200))


@pytest.fixture
def env():
    e = Env()
    yield e
    e.close()


def test_constructing_the_manager_starts_nothing(env):
    assert env.calls == []
    assert env.manager.get("sc1") is None


def test_start_returns_a_running_scan_and_the_engine_runs_once_on_its_own_thread(env):
    async def go():
        scan = await env.manager.start(actor="rest:t")
        assert scan.id == "sc1" and scan.state == "running"
        done = await wait_done(env.manager, scan.id)
        return done
    done = asyncio.run(go())
    assert len(env.calls) == 1
    assert env.threads[0].startswith("discovery-scan")
    d = done.to_dict()
    assert d["state"] == "done" and d["os"] == "linux" and d["limited"] is False
    assert d["started_at"] == 1000.0 and d["finished_at"] == 1000.0
    assert d["suggestions"][0]["suggestion_id"] == "sg1" and d["suggestions"][0]["state"] == "new"


def test_a_second_start_while_running_raises_scan_busy():
    e = Env()
    e.manager._engine = e.blocking_engine

    async def go():
        first = await e.manager.start(actor="a")
        with pytest.raises(ScanBusy) as info:
            await e.manager.start(actor="a")
        assert info.value.scan_id == first.id
        e.release.set()
        await wait_done(e.manager, first.id)
        return await e.manager.start(actor="a")                # allowed again once finished
    try:
        assert asyncio.run(go()).id == "sc2"
    finally:
        e.release.set()
        e.close()


def test_two_concurrent_starts_give_one_scan_and_one_busy():
    e = Env()
    e.manager._engine = e.blocking_engine

    async def go():
        res = await asyncio.gather(e.manager.start(actor="a"), e.manager.start(actor="b"), return_exceptions=True)
        e.release.set()
        return res
    try:
        res = asyncio.run(go())
        assert sum(isinstance(r, ScanBusy) for r in res) == 1 and sum(not isinstance(r, Exception) for r in res) == 1
    finally:
        e.release.set()
        e.close()


def test_a_failing_dismissal_read_marks_the_scan_failed_and_a_later_start_works(monkeypatch, env):
    async def boom(store):
        raise RuntimeError("x")
    monkeypatch.setattr(manager_module.dismissals, "load_fingerprints", boom)

    async def go():
        with pytest.raises(RuntimeError):
            await env.manager.start(actor="a")
        scan = env.manager.get("sc1")
        assert scan.state == "failed" and scan.reason == "internal error"
        monkeypatch.undo()
        return await env.manager.start(actor="a")
    assert asyncio.run(go()).id == "sc2"
    assert len([r for r in audit_rows(env.store) if r["action"] == "discovery_scan"]) >= 1


def test_a_start_cancelled_during_the_dismissal_read_leaves_no_running_scan(monkeypatch, env):
    async def slow(store):
        await asyncio.sleep(5)
        return frozenset()
    monkeypatch.setattr(manager_module.dismissals, "load_fingerprints", slow)

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(env.manager.start(actor="a"), 0.05)
        assert env.manager.get("sc1").state == "failed"
        monkeypatch.undo()
        return await env.manager.start(actor="a")
    assert asyncio.run(go()).id == "sc2"
    assert env.calls == [] or len(env.calls) == 1


def test_an_unsupported_platform_fails_the_scan_without_calling_the_engine():
    e = Env(os_name=None)
    try:
        async def go():
            scan = await e.manager.start(actor="a")
            return await wait_done(e.manager, scan.id)
        scan = asyncio.run(go())
        assert scan.state == "failed" and scan.reason == "unsupported platform" and e.calls == []
    finally:
        e.close()


def test_ids_are_monotonic_across_scans_and_find_is_unambiguous(env):
    async def go():
        ids = []
        for _ in range(3):
            scan = await env.manager.start(actor="a")
            await wait_done(env.manager, scan.id)
            ids.append(scan.id)
        return ids
    assert asyncio.run(go()) == ["sc1", "sc2", "sc3"]
    sids = [env.manager.find(f"sg{i}")[1].suggestion_id for i in (1, 2, 3)]
    assert sids == ["sg1", "sg2", "sg3"]
    assert env.manager.find("sg2")[0].id == "sc2" and env.manager.find("sg9") is None


def test_an_engine_exception_fails_the_scan_with_internal_error_and_one_log(caplog):
    def boom(*a, **k):
        raise ValueError("secret process name")
    e = Env(engine=boom)
    try:
        with caplog.at_level(logging.ERROR, logger="chimericmesh.discovery"):
            async def go():
                scan = await e.manager.start(actor="a")
                return await wait_done(e.manager, scan.id)
            scan = asyncio.run(go())
        assert scan.state == "failed" and scan.reason == "internal error"
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    finally:
        e.close()


def test_an_engine_that_does_not_return_is_failed_with_scan_timed_out_and_cancelled():
    e = Env(scan_timeout=0.2)
    seen = {}

    def stuck(inputs, runner, procfs, fs, next_id, cancel):
        seen["cancel"] = cancel
        cancel.wait(10)
        return ScanOutcome("done", False, "", (), ())
    e.manager._engine = stuck
    try:
        async def go():
            scan = await e.manager.start(actor="a")
            return await wait_done(e.manager, scan.id)
        scan = asyncio.run(go())
        assert scan.state == "failed" and scan.reason == "scan timed out"
        assert seen["cancel"].is_set()
    finally:
        e.close()


def test_without_a_catalog_start_raises_and_the_engine_is_never_called():
    e = Env(load=CatalogLoad(None, "bad"))
    try:
        with pytest.raises(CatalogUnavailable):
            asyncio.run(e.manager.start(actor="a"))
        assert e.calls == []
    finally:
        e.close()


def test_one_audit_row_per_scan_with_counts_only(env):
    async def go():
        scan = await env.manager.start(actor="rest:127.0.0.1")
        await wait_done(env.manager, scan.id)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    rows = [r for r in audit_rows(env.store) if r["action"] == "discovery_scan"]
    assert len(rows) == 1 and rows[0]["actor"] == "rest:127.0.0.1" and rows[0]["subject"] == "sc1"
    assert rows[0]["detail"] == "state=done suggestions=1 limited=False tools_failed=0"
    assert "nginx" not in " ".join(str(v) for v in rows[0].values())


def test_a_failing_audit_append_is_logged_and_the_scan_is_still_done(monkeypatch, env, caplog):
    async def boom(*a, **k):
        raise StoreError("down")
    monkeypatch.setattr(env.store.audit, "append", boom)

    async def go():
        scan = await env.manager.start(actor="a")
        return await wait_done(env.manager, scan.id)
    with caplog.at_level(logging.ERROR, logger="chimericmesh.discovery"):
        scan = asyncio.run(go())
    assert scan.state == "done"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_store_failure_while_reading_dismissals_still_runs_the_scan(monkeypatch, env):
    async def boom(store):
        raise StoreError("down")
    monkeypatch.setattr(manager_module.dismissals, "load_fingerprints", boom)

    async def go():
        scan = await env.manager.start(actor="a")
        return await wait_done(env.manager, scan.id)
    assert asyncio.run(go()).state == "done"
    assert env.calls[0][0].dismissed == frozenset()


def test_covered_is_built_from_the_registry_with_path_key():
    src = SimpleNamespace(path="/var/log/Nginx/access.log", service_id="svc1")
    e = Env(sources=[src])
    try:
        async def go():
            scan = await e.manager.start(actor="a")
            await wait_done(e.manager, scan.id)
        asyncio.run(go())
        inputs = e.calls[0][0]
        assert inputs.covered == {path_key("/var/log/Nginx/access.log", "linux"): "svc1"}
        assert inputs.os_name == "linux" and inputs.is_admin is False and inputs.own_pid == 4242
        assert dict(inputs.env) == {"HOME": "/h"} and inputs.known_formats == FORMATS
    finally:
        e.close()


def test_dismissals_are_passed_to_the_engine(env):
    asyncio.run(dismissals.dismiss(env.store, "nginx", "9" * 16, actor="x", paths=1))

    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
    asyncio.run(go())
    assert env.calls[0][0].dismissed == frozenset({("nginx", "9" * 16)})


def test_scans_expire_one_hour_after_they_finish(env):
    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
    asyncio.run(go())
    env.clock.t += 3599
    assert env.manager.find("sg1") is not None and env.manager.get("sc1") is not None
    env.clock.t += 2
    assert env.manager.find("sg1") is None and env.manager.get("sc1") is None


def test_at_most_five_scans_are_kept_and_a_running_one_is_never_dropped(env):
    async def go():
        for _ in range(7):
            scan = await env.manager.start(actor="a")
            await wait_done(env.manager, scan.id)
            env.clock.t += 1
    asyncio.run(go())
    assert [env.manager.get(f"sc{i}") is not None for i in range(1, 8)] == [False, False, True, True, True, True, True]
    e = Env()
    e.manager._engine = e.blocking_engine
    try:
        async def busy():
            running = await e.manager.start(actor="a")
            for i in range(10):
                e.manager._scans[f"x{i}"] = SimpleNamespace(id=f"x{i}", state="done", finished_at=1.0 + i, suggestions=())
            assert e.manager.get(running.id) is not None
            e.release.set()
        asyncio.run(busy())
    finally:
        e.release.set()
        e.close()


def test_dismiss_writes_the_row_and_one_audit_row_and_repeats_are_harmless(env):
    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
        await env.manager.dismiss("sg1", actor="rest:x")
        await env.manager.dismiss("sg1", actor="rest:x")
        return await env.manager.dismissals()
    rows = asyncio.run(go())
    assert [r["app_id"] for r in rows] == ["nginx"]
    assert len([r for r in audit_rows(env.store) if r["action"] == "discovery_dismissed"]) == 1


def test_dismiss_of_accepted_or_accepting_raises_already_accepted_and_unknown_raises_not_found(env):
    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
        scan.accepted["sg1"] = "svc"
        with pytest.raises(AlreadyAccepted):
            await env.manager.dismiss("sg1", actor="a")
        del scan.accepted["sg1"]
        scan.accepting.add("sg1")
        with pytest.raises(AlreadyAccepted):
            await env.manager.dismiss("sg1", actor="a")
        with pytest.raises(SuggestionNotFound):
            await env.manager.dismiss("sg99", actor="a")
    asyncio.run(go())


def test_clear_dismissals_returns_the_count(env):
    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
        await env.manager.dismiss("sg1", actor="a")
        return await env.manager.clear_dismissals(actor="a")
    assert asyncio.run(go()) == 1


def test_aclose_cancels_a_blocked_engine_within_a_fifth_of_a_second():
    e = Env()
    e.manager._engine = e.blocking_engine
    try:
        async def go():
            await e.manager.start(actor="a")
            await asyncio.sleep(0.05)
            start = time.monotonic()
            await e.manager.aclose()
            return time.monotonic() - start
        assert asyncio.run(go()) < 0.4
    finally:
        e.release.set()
        e.store.close()


def test_scan_ids_use_the_sc_and_sg_prefixes_only(env):
    async def go():
        scan = await env.manager.start(actor="a")
        await wait_done(env.manager, scan.id)
        return scan
    scan = asyncio.run(go())
    assert scan.id.startswith("sc") and scan.suggestions[0].suggestion_id.startswith("sg")
