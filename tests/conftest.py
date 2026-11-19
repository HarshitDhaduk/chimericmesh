"""Shared test isolation: no test may touch the real home directory or inherit CHIMERICMESH_DB."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_store_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_HOME", str(tmp_path / "cm-home"))
    monkeypatch.delenv("CHIMERICMESH_DB", raising=False)
    for name in (
        "CHIMERICMESH_MAX_EVENTS",
        "CHIMERICMESH_EVENT_TTL_DAYS",
        "CHIMERICMESH_REOPEN_AFTER_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeClock:
    """Callable clock that tests advance by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def svc_store(tmp_path):
    """A file store opened with the core and services migrations (lazy imports: see task 3.1)."""
    from chimericmesh.services.schema import services_registry
    from chimericmesh.store import Store

    store = Store.open(tmp_path / "svc.db", registry=services_registry())
    yield store
    store.close()


@pytest.fixture
def svc_mem_store():
    """A private ``:memory:`` store opened with the core and services migrations."""
    from chimericmesh.services.schema import services_registry
    from chimericmesh.store import Store

    store = Store.open(":memory:", registry=services_registry())
    yield store
    store.close()


@pytest.fixture
def make_registry():
    """Factory: ``make_registry(store, hub=None, **kw)`` builds a ``ServiceRegistry`` for a store and a hub."""

    def factory(store, hub=None, **kw):
        from chimericmesh.hub import Hub
        from chimericmesh.services.registry import ServiceRegistry

        return ServiceRegistry.load(store, state=hub if hub is not None else Hub(), **kw)

    return factory
