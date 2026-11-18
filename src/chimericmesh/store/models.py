"""Proposal model shared by the in-memory hub and the store."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

BASELINE_KEYS = ("id", "incident_id", "action", "details", "status", "created", "decided_by",
                 "decided_at", "result_note")


@dataclass
class Proposal:
    """A remediation an agent wants to perform, awaiting a human decision.

    SAFETY: ChimericMesh never executes ``action`` on any host. It only records the
    human decision and announces it on IRC. The agent (Kiro via MCP) does the real work
    under its own tool permissions, and only after the proposal is approved.
    """

    id: str
    incident_id: str
    action: str
    details: str = ""
    status: str = "pending"  # pending -> approved|denied ; approved -> done
    created: float = field(default_factory=time.time)
    decided_by: str = ""
    decided_at: float | None = None
    result_note: str = ""
    # Extended fields (nullable, so rows written by older versions still load).
    decision_origin: str | None = None
    service_id: str | None = None
    incident_opening: int | None = None
    approved_by_rule: str | None = None
    kind: str | None = None
    files: list[str] | None = None
    estimated_lines: int | None = None
    verification: str | None = None
    files_changed: list[str] | None = None
    lines_changed: int | None = None
    verification_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The nine baseline keys, unchanged."""
        full = asdict(self)
        return {k: full[k] for k in BASELINE_KEYS}

    def to_full_dict(self) -> dict[str, Any]:
        return asdict(self)
