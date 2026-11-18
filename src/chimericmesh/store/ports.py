"""Protocols that let other modules use the store without importing the hub."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, Protocol, runtime_checkable

from chimericmesh.diagnostics import Incident
from chimericmesh.events import Event

from .models import Proposal

Announce = Callable[[str], Awaitable[None]]


class AuditSink(Protocol):
    async def append(self, actor: str, action: str, subject: str, detail: str = "", *,
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> object: ...


@runtime_checkable
class ProposalClient(Protocol):
    """What the agent loop gets: create and complete only."""

    async def create_proposal(self, incident_id: str, action: str, details: str = "", *,
                              extra: Mapping[str, object] | None = None) -> Proposal | None: ...

    async def complete(self, pid: str, note: str = "", *,
                       extra: Mapping[str, object] | None = None) -> Proposal | None: ...


@runtime_checkable
class ProposalWriter(ProposalClient, Protocol):
    """Write seam for proposals. ``Hub`` implements it in both modes."""

    async def decide(self, pid: str, approve: bool, by: str) -> Proposal | None: ...

    async def hub_transition(self, pid: str, new_status: str, *, decided_by: str,
                             approved_by_rule: str | None = None, result_note: str | None = None,
                             extra: Mapping[str, object] | None = None) -> Proposal | None: ...


class StateView(Protocol):
    """Read-only view of hub state. ``Hub`` implements it in both modes."""

    def now(self) -> float: ...

    def get_incident(self, incident_id: str) -> Incident | None: ...

    def incidents(self, *, status: Literal["open", "resolved"] | None = None,
                  service_id: str | None = None, limit: int | None = None) -> list[Incident]: ...

    def get_proposal(self, pid: str) -> Proposal | None: ...

    def list_proposals(self, status: str | None = None, *, service_id: str | None = None,
                       limit: int | None = None) -> list[Proposal]: ...

    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]: ...

    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]: ...
