"""The injected interfaces of the agent loop (review M2). This module never imports the hub."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chimericmesh.approvals.models import ApprovalStatus, Rule
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store.models import Proposal
from chimericmesh.store.ports import StateView


@runtime_checkable
class ApprovalReader(Protocol):
    """A three-method protocol that ``ApprovalManager`` already satisfies. Nothing here can create,
    patch, enable, resume, revoke or pause a rule (R1-L2)."""

    def approval_of(self, prop: Proposal) -> dict | None: ...

    def rule(self, rule_id: str) -> Rule | None: ...

    def status(self) -> ApprovalStatus: ...


@dataclass(frozen=True, slots=True)
class AgentPorts:
    state: StateView
    registry: ServiceRegistry
    approvals: ApprovalReader | None     # None only in unit tests; every rule approval then fails verify
    protected_dirs: Sequence[str]
    workspace_root: str | None           # snippet only
