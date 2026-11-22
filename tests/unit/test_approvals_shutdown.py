"""Shutdown cleanliness of create_app with approvals on, without help from the garbage collector.

FastAPI apps are themselves reference cycles, so a dropped app is only freed by the collector. What the normal
shutdown (the lifespan) must guarantee is that no store writer thread outlives it and that a hub the caller keeps
no longer reaches the closed approval manager. The collector is disabled while the checks run.
"""
import asyncio
import gc
import os
import threading
import types
from collections import deque

import pytest

from chimericmesh.api import build_registry, create_app
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.store import Store

WRITER = "store-writer"
MSG = "could not write block 1 No space left on device"


def writers() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == WRITER and t.is_alive()}


def reaches(root: object, target: object, limit: int = 200_000) -> bool:
    """Whether ``root`` holds ``target`` through strong references. Modules, classes and function globals are
    not followed: they reach everything and say nothing about what the hub holds."""
    seen = {id(root)}
    todo = deque([root])
    while todo and len(seen) < limit:
        obj = todo.popleft()
        if obj is target:
            return True
        if isinstance(obj, types.FunctionType):
            nxt = [c for c in (obj.__closure__ or ())] + list(obj.__defaults__ or ())
        elif isinstance(obj, (types.ModuleType, type, types.CodeType, types.FrameType)):
            continue
        else:
            nxt = gc.get_referents(obj)
        for ref in nxt:
            if id(ref) not in seen:
                seen.add(id(ref))
                todo.append(ref)
    assert len(seen) < limit, "reference walk did not finish"
    return False


@pytest.fixture
def no_gc():
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


@pytest.mark.parametrize("mode", ["bare", "store"])
def test_normal_shutdown_stops_the_writer_and_releases_the_manager(mode, tmp_path, monkeypatch, no_gc):
    monkeypatch.setenv("CHIMERICMESH_HOME", str(tmp_path / "home"))
    before = writers()
    store = Store.open(os.path.join(tmp_path, "hub.db"), registry=build_registry()) if mode == "store" else None
    hub = Hub(store=store) if store is not None else Hub()
    app = create_app(hub, start_listeners=False, enforce_access=False)
    manager = app.state.approvals
    app_store = app.state.store
    assert manager is not None and hub.approver is manager and hub.approvals is manager
    assert writers() - before, "the app's store should have a writer thread while it runs"

    async def run():
        async with app.router.lifespan_context(app):
            for _ in range(3):
                await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0))
            incident = hub.incidents()[0]
            await hub.create_proposal(incident.id, "fix", "details")
            await manager.expire_due()

    asyncio.run(run())

    assert hub.approver is None and hub.approvals is None  # fails closed: nothing approves after shutdown
    assert manager._sweep_task is None and all(t.done() for t in manager._tasks)
    assert not reaches(hub, manager), "the kept hub still reaches the closed approval manager"
    if store is not None:
        assert not store._closed  # the caller owns it
        store.close()
    else:
        assert app_store._closed  # the private store is closed by the lifespan
    del app, manager, app_store
    assert not (writers() - before), "a store writer thread outlived the shutdown"
