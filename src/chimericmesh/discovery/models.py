"""Immutable records and constants of the discovery module. There is no command line, environment or
process list in any public type (R2.7)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .catalog import Catalog

SCAN_TOTAL_SECONDS = 30.0
TOOL_TIMEOUT_SECONDS = 10.0
MAX_TOOL_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_NAMES_PER_PATTERN = 200
MAX_SUGGESTIONS = 50
MAX_PATHS_PER_SUGGESTION = 25
MAX_PROCESS_NAME = 64
SCAN_TTL_SECONDS = 3600.0
MAX_KEPT_SCANS = 5
MAX_DISMISSALS = 500
PORT_ONLY_PORTS = frozenset({21, 23, 25, 110, 143, 161, 465, 514, 587, 993, 995, 3128})


@dataclass(frozen=True, slots=True)
class ProcEntry:
    pid: int | None
    name: str


@dataclass(frozen=True, slots=True)
class Listener:
    port: int
    proto: Literal["tcp", "udp"]
    pid: int | None
    name: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    procs: tuple[ProcEntry, ...]
    listeners: tuple[Listener, ...]
    limited: bool
    failed_tools: tuple[str, ...]
    usable: bool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunResult:
    ok: bool
    stdout: str
    reason: Literal["", "missing", "denied", "timeout", "error", "cancelled"]
    truncated: bool = False
    returncode: int | None = None


@dataclass(frozen=True, slots=True)
class CandidatePath:
    path: str
    format: str
    kind: str
    exists: bool
    readable: bool
    size_bytes: int | None
    watched_by: str | None

    def to_dict(self) -> dict:
        return {"path": self.path, "format": self.format, "kind": self.kind, "exists": self.exists,
                "readable": self.readable, "size_bytes": self.size_bytes, "watched_by": self.watched_by}


@dataclass(frozen=True, slots=True)
class Evidence:
    process: str | None
    pid: int | None
    process_count: int
    ports: tuple[int, ...]

    def to_dict(self) -> dict:
        return {"process": self.process, "pid": self.pid, "process_count": self.process_count,
                "ports": list(self.ports)}


@dataclass(frozen=True, slots=True)
class Suggestion:
    suggestion_id: str
    app_id: str
    name: str
    protocol_label: str
    confidence: Literal["high", "low"]
    evidence: Evidence
    candidates: tuple[CandidatePath, ...]
    fingerprint: str

    def to_dict(self, *, state: str, service_id: str | None) -> dict:
        # No code_folder here, ever (O16): discovery never proposes a code folder.
        return {
            "suggestion_id": self.suggestion_id, "app_id": self.app_id, "name": self.name,
            "protocol_label": self.protocol_label, "confidence": self.confidence,
            "evidence": self.evidence.to_dict(),
            "candidate_log_paths": [c.to_dict() for c in self.candidates],
            "state": state, "service_id": service_id,
        }


@dataclass(slots=True)
class Scan:
    """Mutable; touched on the loop thread only."""
    id: str
    state: Literal["running", "done", "failed"]
    started_at: float
    finished_at: float | None
    limited: bool
    os: str
    reason: str
    failed_tools: tuple[str, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    accepted: dict[str, str] = field(default_factory=dict)
    accepting: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "state": self.state, "started_at": self.started_at,
            "finished_at": self.finished_at, "limited": self.limited, "os": self.os,
            "reason": self.reason, "failed_tools": list(self.failed_tools),
            "suggestions": [
                s.to_dict(state="accepted" if s.suggestion_id in self.accepted else "new",
                          service_id=self.accepted.get(s.suggestion_id))
                for s in self.suggestions],
        }


@dataclass(frozen=True, slots=True)
class ScanInputs:
    os_name: str
    is_admin: bool
    own_pid: int
    env: Mapping[str, str]
    catalog: "Catalog"
    covered: Mapping[str, str]
    dismissed: frozenset[tuple[str, str]]
    known_formats: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    state: Literal["done", "failed"]
    limited: bool
    reason: str
    failed_tools: tuple[str, ...]
    suggestions: tuple[Suggestion, ...]
