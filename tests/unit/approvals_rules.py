"""Builders for rules and match contexts shared by the auto-approval tests."""
from __future__ import annotations

import dataclasses

from chimericmesh.approvals.matching import MatchContext, RuleUsage
from chimericmesh.approvals.models import Rule

NOW = 1_700_000_000.0


def make_rule(**kw) -> Rule:
    base = dict(id="r1", seq=1, service_id="api-gw", match_type="signature", match_value="disk full",
                most_urgent_severity=3, expires_at=NOW + 86400, max_per_hour=3, max_per_incident=1, max_files=5,
                max_lines=100, status="active", status_reason="", created_by="rest:127.0.0.1", created_at=NOW - 10,
                updated_at=NOW - 10, source_proposal_id=None, note="", use_count=0, code_folder="/code")
    base.update(kw)
    return Rule(**base)


def make_ctx(**kw) -> MatchContext:
    base = dict(now=NOW, paused=False, kind="code_change", service_id="api-gw", signature="disk full",
                category="disk", severity=3, files=("a.py",), estimated_lines=10, verification="run tests",
                file_problem=None, live_code_folder="/code", cooldown_active=False)
    base.update(kw)
    return MatchContext(**base)


def make_usage(hour: int = 0, incident_opening: int = 0) -> RuleUsage:
    return RuleUsage(hour=hour, incident_opening=incident_opening)


def replace(obj, **kw):
    return dataclasses.replace(obj, **kw)
