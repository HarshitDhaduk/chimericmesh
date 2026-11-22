"""Rule, request and plan models plus the constants every other file shares."""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from chimericmesh.store.models import Proposal
from chimericmesh.store.tx import Tx

from .limits import MAX_PROPOSALS_PER_SERVICE_HOUR

log = logging.getLogger("chimericmesh.approvals")

# A test pins this set against the categories diagnostics can emit.
CATEGORIES = ("memory", "disk", "network", "security", "crash", "unknown")
MATCH_TYPES = ("signature", "category")
RULE_STATUSES = ("active", "suspended", "revoked", "expired")
VERIFICATION_RESULTS = ("passed", "failed", "not_run")
MAX_RULES_PER_SERVICE, MAX_RULES_TOTAL = 20, 100

DAY = 86400.0
DEFAULT_EXPIRY_SECONDS = 7 * DAY
MAX_EXPIRY_SECONDS = 30 * DAY
RULE_APPROVAL_TTL = 1800.0          # a rule approval stays usable this long (R6.4)
HOUR = 3600.0

BOUNDS: dict[str, dict[str, int]] = {
    "most_urgent_severity": {"min": 3, "max": 4, "default": 3},
    "max_per_hour": {"min": 1, "max": MAX_PROPOSALS_PER_SERVICE_HOUR, "default": 3},
    "max_per_incident": {"min": 1, "max": 3, "default": 1},
    "max_files": {"min": 1, "max": 20, "default": 5},
    "max_lines": {"min": 1, "max": 500, "default": 100},
    "expires_days": {"min": 0, "max": 30, "default": 7},
}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    seq: int
    service_id: str
    match_type: str
    match_value: str
    most_urgent_severity: int
    expires_at: float
    max_per_hour: int
    max_per_incident: int
    max_files: int
    max_lines: int
    status: str
    status_reason: str
    created_by: str
    created_at: float
    updated_at: float
    source_proposal_id: str | None
    note: str
    use_count: int
    code_folder: str

    def to_dict(self, *, now: float, uses_last_hour: int) -> dict[str, Any]:
        status = self.status
        if status in ("active", "suspended") and now >= self.expires_at:
            status = "expired"   # computed on read, before the sweep has run
        return {
            "id": self.id, "service_id": self.service_id, "match_type": self.match_type,
            "match_value": self.match_value, "most_urgent_severity": self.most_urgent_severity,
            "expires_at": self.expires_at, "max_per_hour": self.max_per_hour,
            "max_per_incident": self.max_per_incident, "max_files": self.max_files,
            "max_lines": self.max_lines, "status": status, "status_reason": self.status_reason,
            "created_by": self.created_by, "created_at": self.created_at, "updated_at": self.updated_at,
            "source_proposal_id": self.source_proposal_id, "note": self.note, "use_count": self.use_count,
            "code_folder": self.code_folder, "uses_last_hour": uses_last_hour,
            "remaining_this_hour": max(0, self.max_per_hour - uses_last_hour),
        }


@dataclass(frozen=True, slots=True)
class RuleFields:
    """Create input, already validated."""
    service_id: str
    match_type: str
    match_value: str
    most_urgent_severity: int = 3
    expires_at: float | None = None
    max_per_hour: int = 3
    max_per_incident: int = 1
    max_files: int = 5
    max_lines: int = 100
    note: str = ""


@dataclass(frozen=True, slots=True)
class RulePatch:
    """None means unchanged."""
    most_urgent_severity: int | None = None
    expires_at: float | None = None
    max_per_hour: int | None = None
    max_per_incident: int | None = None
    max_files: int | None = None
    max_lines: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RuleDefaults:
    service_id: str
    match_type: str
    match_value: str
    most_urgent_severity: int
    expires_at: float
    max_per_hour: int
    max_per_incident: int
    max_files: int
    max_lines: int
    note: str
    source_proposal_id: str

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(frozen=True, slots=True)
class CreateContext:
    """Built by ``Hub.create_proposal`` on the loop thread."""
    incident_id: str
    service_id: str | None
    signature: str
    category: str
    severity: int
    opening: int
    action: str
    details: str
    extra: Mapping[str, object]
    enforce_limits: bool


@dataclass(frozen=True, slots=True)
class RuleCheck:
    rule_id: str
    failed: str | None   # a condition code, or None for the rule that matched


AuditAppend = Callable[..., int]


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    """Immutable; crosses to the writer thread."""
    kind: Literal["pending", "auto", "cooldown"]
    extra: Mapping[str, object]
    rule_id: str | None
    checks: tuple[RuleCheck, ...]
    incident_id: str
    service_id: str | None
    opening: int
    files: tuple[str, ...]
    remaining_hour: int | None
    remaining_incident: int | None
    prior_proposal_id: str | None
    reserved_at: float
    opening_first_seen: float | None
    token: int
    limit_token: int = 0
    paused_skip: bool = False
    audit_append: AuditAppend | None = field(default=None, compare=False, repr=False)

    def write_in_tx(self, tx: Tx, prop: Proposal) -> None:
        """Store-backed commit work, on the writer thread, using only ``tx`` and this plan."""
        from .rulebook import insert_opening, insert_use, bump_use_count  # local: keeps models import-light

        audit = self.audit_append
        if self.opening_first_seen is not None:
            insert_opening(tx, self.incident_id, self.opening, self.opening_first_seen)
        if self.kind == "auto" and self.rule_id is not None:
            insert_use(tx, rule_id=self.rule_id, proposal_id=prop.id, incident_id=self.incident_id,
                       opening=self.opening, service_id=self.service_id, approved_at=self.reserved_at)
            bump_use_count(tx, self.rule_id)
        if audit is None:
            return
        sid = self.service_id
        if self.kind == "auto" and self.rule_id is not None:
            files = ",".join(self.files)
            audit(tx, f"rule:{self.rule_id}", "proposal_auto_approved", prop.id,
                  f"incident={self.incident_id} files={files} remaining_hour={self.remaining_hour} "
                  f"remaining_incident={self.remaining_incident}",
                  service_id=sid, rule_id=self.rule_id, proposal_id=prop.id)
        elif self.kind == "cooldown":
            rule = self.rule_id
            audit(tx, "hub", "autofix_cooldown", prop.id,
                  f"earlier={self.prior_proposal_id} rule={rule} reopened_at={self.opening_first_seen}",
                  service_id=sid, rule_id=rule, proposal_id=prop.id)
        else:
            if self.paused_skip:
                audit(tx, "hub", "auto_approval_skipped", prop.id, "condition=paused",
                      service_id=sid, proposal_id=prop.id)
            for check in self.checks:
                if check.failed is not None:
                    audit(tx, "hub", "auto_approval_skipped", prop.id, f"condition={check.failed}",
                          service_id=sid, rule_id=check.rule_id, proposal_id=prop.id)


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    paused: bool
    paused_by: str
    paused_at: float | None
    active_rules: int
    suspended_rules: int
    cooldown_seconds: float
    proposal_ttl_hours: float
    bounds: Mapping[str, Mapping[str, int]] = field(default_factory=lambda: BOUNDS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused, "paused_by": self.paused_by, "paused_at": self.paused_at,
            "active_rules": self.active_rules, "suspended_rules": self.suspended_rules,
            "cooldown_seconds": self.cooldown_seconds, "proposal_ttl_hours": self.proposal_ttl_hours,
            "bounds": {k: dict(v) for k, v in self.bounds.items()},
        }


@dataclass(frozen=True, slots=True)
class ApprovalSettings:
    autofix_cooldown: float = 86400.0
    proposal_ttl: float = 24 * 3600.0
    rule_approval_ttl: float = RULE_APPROVAL_TTL
    sweep_interval: float = 60.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ApprovalSettings":
        cooldown = _env_number(environ, "CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS", 86400.0, 0.0, 2_592_000.0)
        ttl_hours = _env_number(environ, "CHIMERICMESH_PROPOSAL_TTL_HOURS", 24.0, 1.0, 720.0)
        return cls(autofix_cooldown=cooldown, proposal_ttl=ttl_hours * 3600.0)


def _env_number(environ: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default
    if not math.isfinite(value) or not low <= value <= high:
        log.warning("%s is outside %s to %s; using %s", name, low, high, default)
        return default
    return value
