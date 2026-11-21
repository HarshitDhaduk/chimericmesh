"""``create_app`` wiring of auto-approval (task 8.1)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time

import pytest

from chimericmesh import api
from chimericmesh.api import build_registry, create_app
from chimericmesh.approvals.errors import ApprovalsNotMigrated
from chimericmesh.approvals.manager import ApprovalManager
from chimericmesh.connectors.schema import connectors_registry
from chimericmesh.discovery.schema import register_migrations as register_discovery
from chimericmesh.hub import Hub
from chimericmesh.store import Store


@pytest.fixture()
def home(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(tmp.name, "home"))
    monkeypatch.delenv("CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS", raising=False)
    yield tmp.name
    tmp.cleanup()


def test_create_app_with_a_bare_hub_wires_the_manager(home):
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    manager = app.state.approvals
    assert isinstance(manager, ApprovalManager) and hub.approver is manager and hub.approvals is manager
    assert hub.enforce_limits is False                         # a bare Hub keeps its baseline behaviour
    assert manager._state is hub and manager._writer is hub
    assert manager._registry is app.state.services and manager._limiter is app.state.reduce_limiter
    assert manager.shares_store(hub.store) is False and manager.shares_store(app.state.store) is True


def test_create_app_with_a_store_backed_hub_turns_the_limits_on(home):
    os.makedirs(os.path.join(home, "db"))
    store = Store.open(os.path.join(home, "db", "w.db"), registry=build_registry())
    try:
        app = create_app(store=store, start_listeners=False)
        hub = app.state.hub
        assert hub.store is store and hub.enforce_limits is True and hub.approver is app.state.approvals
        assert app.state.approvals.shares_store(hub.store) is True
        # the same wiring when the hub is passed in
        hub2 = Hub(store=store)
        assert create_app(hub2, start_listeners=False).state.hub.enforce_limits is True
    finally:
        store.close()


def test_the_approvals_migrations_come_after_services_and_connectors():
    versions = {m.module: m.version for m in build_registry().migrations()}
    assert versions["services"] < versions["connectors"] < versions["approvals"] == 400
    assert any(m.name == "approvals:0400-rules" for m in build_registry().migrations())


def test_a_store_without_the_approvals_migrations_is_refused(home):
    registry = connectors_registry()
    register_discovery(registry)
    store = Store.open(":memory:", registry=registry)
    try:
        with pytest.raises(ApprovalsNotMigrated):
            create_app(store=store, start_listeners=False)
    finally:
        store.close()


def test_the_sweep_task_runs_in_the_lifespan_and_stops_with_it(home):
    app = create_app(Hub(), start_listeners=False)
    manager = app.state.approvals

    async def go():
        async with app.router.lifespan_context(app):
            assert manager._sweep_task is not None and not manager._sweep_task.done()
            task = manager._sweep_task
        assert manager._sweep_task is None and task.done()

    started = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - started < api.SHUTDOWN_SECONDS


def test_shutdown_stays_inside_its_deadline_with_a_failing_store(home):
    os.makedirs(os.path.join(home, "db"))
    store = Store.open(os.path.join(home, "db", "s.db"), registry=build_registry())
    app = create_app(store=store, start_listeners=False)

    async def go():
        async with app.router.lifespan_context(app):
            async def hang(fn):
                await asyncio.sleep(60)

            store.transact = hang

    started = time.monotonic()
    try:
        try:
            asyncio.run(go())
        except Exception:
            pass
    finally:
        store.close(timeout=1.0)
    assert time.monotonic() - started < api.SHUTDOWN_SECONDS + 3


def test_a_bad_cooldown_value_warns_once_and_the_app_still_builds(home, monkeypatch, caplog):
    monkeypatch.setenv("CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS", "abc")
    with caplog.at_level(logging.WARNING, logger="chimericmesh.approvals"):
        app = create_app(Hub(), start_listeners=False)
    warnings = [r for r in caplog.records if "AUTOFIX_COOLDOWN" in r.getMessage()]
    assert len(warnings) == 1
    assert app.state.approvals.status().cooldown_seconds == 86400


def test_the_cooldown_comes_from_the_environment(home, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS", "120")
    monkeypatch.setenv("CHIMERICMESH_PROPOSAL_TTL_HOURS", "2")
    st = create_app(Hub(), start_listeners=False).state.approvals.status()
    assert st.cooldown_seconds == 120 and st.proposal_ttl_hours == 2
