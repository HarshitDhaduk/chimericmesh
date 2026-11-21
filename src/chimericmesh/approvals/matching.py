"""Rule matching. Pure: no clock, store or registry is read here.

The requirement lists conditions (a) to (m); they are numbered 4.2.1 to 4.2.13 in the order below
(design L8). The first failing condition is the one returned.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .models import Rule, RuleCheck


class Condition(str, Enum):
    PAUSED = "paused"                  # 4.2.1  (a)
    INACTIVE = "inactive"              # 4.2.2  (b) status active and now < expires_at
    KIND = "kind"                      # 4.2.3  (c)
    SERVICE = "service"                # 4.2.4  (d)
    MATCH = "match"                    # 4.2.5  (e)
    SEVERITY = "severity"              # 4.2.6  (f)
    HOUR_QUOTA = "hour_quota"          # 4.2.7  (g)
    INCIDENT_QUOTA = "incident_quota"  # 4.2.8  (h)
    FILES = "files"                    # 4.2.9  (i)
    LINES = "lines"                    # 4.2.10 (j)
    VERIFICATION = "verification"      # 4.2.11 (k)
    CODE_FOLDER = "code_folder"        # 4.2.12 (l)
    COOLDOWN = "cooldown"              # 4.2.13 (m)


RULE_CONDITIONS = tuple(Condition)
ERROR = "error"


@dataclass(frozen=True, slots=True)
class RuleUsage:
    hour: int
    incident_opening: int


@dataclass(frozen=True, slots=True)
class MatchContext:
    now: float
    paused: bool
    kind: str | None
    service_id: str | None
    signature: str
    category: str
    severity: int
    files: tuple[str, ...] | None
    estimated_lines: int | None
    verification: str | None
    file_problem: str | None         # first FileProblem code for ``files``, computed once per proposal
    live_code_folder: str | None     # the service's code folder right now
    cooldown_active: bool


def match_rule(rule: Rule, ctx: MatchContext, usage: RuleUsage) -> str | None:
    """None when the rule covers the proposal, else the code of the first failing condition. Never raises."""
    try:
        return _match(rule, ctx, usage)
    except Exception:  # noqa: BLE001 - total: a surprise means "no match"
        return ERROR


def _match(rule: Rule, ctx: MatchContext, usage: RuleUsage) -> str | None:
    if ctx.paused:
        return Condition.PAUSED.value
    if rule.status != "active" or ctx.now >= rule.expires_at:
        return Condition.INACTIVE.value
    if ctx.kind != "code_change":
        return Condition.KIND.value
    if ctx.service_id is None or ctx.service_id != rule.service_id:
        return Condition.SERVICE.value
    wanted = ctx.signature if rule.match_type == "signature" else ctx.category
    if rule.match_type not in ("signature", "category") or wanted != rule.match_value:
        return Condition.MATCH.value
    if ctx.severity < max(3, rule.most_urgent_severity):
        return Condition.SEVERITY.value
    if usage.hour >= rule.max_per_hour:
        return Condition.HOUR_QUOTA.value
    if usage.incident_opening >= rule.max_per_incident:
        return Condition.INCIDENT_QUOTA.value
    if (not ctx.files or len(ctx.files) > rule.max_files or ctx.file_problem is not None
            or not all(isinstance(f, str) for f in ctx.files)):
        return Condition.FILES.value
    if ctx.estimated_lines is None or ctx.estimated_lines > rule.max_lines:
        return Condition.LINES.value
    if not (ctx.verification or "").strip():
        return Condition.VERIFICATION.value
    if ctx.live_code_folder is None or ctx.live_code_folder != rule.code_folder:
        return Condition.CODE_FOLDER.value
    if ctx.cooldown_active:
        return Condition.COOLDOWN.value
    return None


def first_match(rules: Sequence[Rule], ctx: MatchContext,
                usage_of: Callable[[str], RuleUsage]) -> tuple[Rule | None, tuple[RuleCheck, ...]]:
    """Walk the rules by ``seq``; stop at the first that matches. ``checks`` lists every rule examined."""
    checks: list[RuleCheck] = []
    for rule in sorted(rules, key=lambda r: r.seq):
        failed = match_rule(rule, ctx, usage_of(rule.id))
        checks.append(RuleCheck(rule.id, failed))
        if failed is None:
            return rule, tuple(checks)
    return None, tuple(checks)
