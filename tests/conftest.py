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
