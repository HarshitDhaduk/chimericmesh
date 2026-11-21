"""Harness for the auto-approval tests: a manager over a bare or a store-backed hub, with a fake clock."""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chimericmesh.approvals.manager import ApprovalManager
from chimericmesh.approvals.models import ApprovalSettings
from chimericmesh.approvals.schema import approvals_registry
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import ServiceFields
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store

START = 1_700_000_000.0


class FakeClock:
    def __init__(self, start: float = START) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class FakeLimiter:
    """Records every ``record`` call; ``allow`` decides the announce answer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.allow = True
        self.audit: Any = None   # set to a store AuditLog to also write rows like the real one

    async def record(self, key: str, *, actor: str, action: str, subject: str, detail: str = "",
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> bool:
        self.calls.append({"key": key, "actor": actor, "action": action, "subject": subject, "detail": detail,
                           "service_id": service_id, "rule_id": rule_id, "proposal_id": proposal_id})
        if self.audit is not None:
            await self.audit.append(actor, action, subject, detail, service_id=service_id, rule_id=rule_id,
                                    proposal_id=proposal_id)
        return self.allow

    async def aclose(self) -> None:
        return None


@dataclass
class Rig:
    mode: str
    hub: Hub
    store: Store
    registry: ServiceRegistry
    manager: ApprovalManager
    clock: FakeClock
    limiter: Any
    announced: list[str] = field(default_factory=list)
    folder: str = ""
    tmp: Any = None

    async def add_service(self, sid: str = "api-gw", *, folder: str | None = "default",
                          hosts: tuple[str, ...] = ("web01",)) -> str:
        code = self.folder if folder == "default" else folder
        await self.registry.create_service(
            ServiceFields(id=sid, name=sid, hosts=hosts, code_folder=code), actor="test")
        return sid

    async def audit(self, **filters) -> list[dict]:
        return await self.store.audit.read(500, **filters)

    async def actions(self, **filters) -> list[str]:
        return [row["action"] for row in reversed(await self.audit(**filters))]

    def close(self) -> None:
        try:
            asyncio.get_event_loop_policy()
        finally:
            self.store.close()
            if self.tmp is not None:
                self.tmp.cleanup()


def make_manager(mode: str = "bare", *, limiter: Any = None, settings: ApprovalSettings | None = None,
                 clock: FakeClock | None = None, enforce_limits: bool = False) -> Rig:
    """``mode`` is ``bare`` (hub memory, private store) or ``store`` (file store under the hub)."""
    clock = clock or FakeClock()
    tmp = tempfile.TemporaryDirectory()
    folder = os.path.realpath(os.path.join(tmp.name, "code"))
    os.makedirs(folder, exist_ok=True)
    if mode == "store":
        store = Store.open(str(Path(tmp.name) / "hub.db"), registry=approvals_registry())
        hub = Hub(store=store, clock=clock)
    else:
        store = Store.open(":memory:", registry=approvals_registry())
        hub = Hub(clock=clock)
    hub.enforce_limits = enforce_limits
    registry = ServiceRegistry.load(store, state=hub, protected=(), clock=clock)
    hub.tagger = registry.tag
    limiter = limiter if limiter is not None else FakeLimiter()
    announced: list[str] = []

    async def announce(text: str) -> None:
        announced.append(text)

    manager = ApprovalManager.load(
        store, state=hub, writer=hub, registry=registry, limiter=limiter, announce=announce,
        settings=settings or ApprovalSettings(), now=clock)
    hub.approver = manager
    hub.approvals = manager
    rig = Rig(mode, hub, store, registry, manager, clock, limiter, announced, folder, tmp)
    return rig


async def open_incident(hub: Hub, *, host: str = "web01", message: str = "disk full on /var", count: int = 3,
                        severity: int = 3, source: str = "syslog") -> str:
    """Ingest enough identical events to open an incident and return its id."""
    event = Event(id=0, ts=hub.now(), source=source, host=host, severity=severity, message=message, app="app")
    iid = hub.tracker.preview_id(hub.tagger(event) if hub.tagger else event)
    assert iid is not None, "the event would be ignored"
    for _ in range(count):
        await hub.ingest(event)
    assert hub.get_incident(iid) is not None, "no incident opened"
    return iid


@pytest.fixture(autouse=True)
def collect_garbage():
    """Apps built by a test hold reference cycles; collect them so no private store keeps a writer thread alive."""
    yield
    import gc

    gc.collect()
