"""The seam the hub uses: ``Hub`` knows this protocol, never the manager (M2)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from chimericmesh.store import Store
from chimericmesh.store.models import Proposal

from .models import ApprovalPlan, CreateContext

AnnounceFn = Callable[[str], Awaitable[None]]
ClockFn = Callable[[], float]


class ProposalApprover(Protocol):
    def prepare(self, ctx: CreateContext) -> ApprovalPlan:
        """Synchronous and await-free. Raises ProposalInvalid, ActiveProposalExists, ProposalRateLimited."""

    def shares_store(self, store: Store | None) -> bool: ...

    async def persist(self, plan: ApprovalPlan, prop: Proposal) -> None:
        """Bare hub: own transaction. Never raises."""

    def confirm(self, plan: ApprovalPlan, prop: Proposal) -> str | None:
        """Bind the reservation, then re-read the rule. A ``decided_by`` denial text, or None."""

    def abort(self, plan: ApprovalPlan) -> None: ...

    async def before_complete(self, pid: str) -> None: ...

    def validate_completion(self, pid: str, extra: Mapping[str, object] | None) -> Mapping[str, object]: ...

    async def after_complete(self, prop: Proposal, extra: Mapping[str, object]) -> None: ...

    def line_for(self, plan: ApprovalPlan, base_line: str, prop: Proposal) -> str: ...
