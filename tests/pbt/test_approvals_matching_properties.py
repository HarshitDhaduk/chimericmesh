"""Properties 1 to 4 of auto-approval: matching."""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_rules import NOW, make_rule  # noqa: E402

from chimericmesh.approvals.matching import MatchContext, RuleUsage, first_match, match_rule  # noqa: E402

SETTINGS = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])

contexts = st.builds(
    MatchContext,
    now=st.floats(min_value=NOW - 1000, max_value=NOW + 200_000, allow_nan=False),
    paused=st.booleans(),
    kind=st.sampled_from(["code_change", "code_change", None, "other"]),
    service_id=st.sampled_from(["api-gw", "api-gw", "other", None]),
    signature=st.sampled_from(["disk full", "disk full", "x"]),
    category=st.sampled_from(["disk", "memory"]),
    severity=st.integers(min_value=0, max_value=7),
    files=st.one_of(st.none(), st.lists(st.sampled_from(["a.py", "b.py", "c.py"]), max_size=8).map(tuple)),
    estimated_lines=st.one_of(st.none(), st.integers(min_value=0, max_value=700)),
    verification=st.sampled_from([None, "", " ", "run tests"]),
    file_problem=st.sampled_from([None, None, None, "outside_folder"]),
    live_code_folder=st.sampled_from(["/code", "/code", "/other", None]),
    cooldown_active=st.booleans(),
)
usages = st.builds(RuleUsage, hour=st.integers(0, 12), incident_opening=st.integers(0, 5))
rules = st.builds(
    make_rule,
    status=st.sampled_from(["active", "active", "suspended", "revoked", "expired"]),
    match_type=st.sampled_from(["signature", "category"]),
    match_value=st.sampled_from(["disk full", "disk", "x"]),
    most_urgent_severity=st.sampled_from([3, 4]),
    max_per_hour=st.integers(1, 10), max_per_incident=st.integers(1, 3),
    max_files=st.integers(1, 20), max_lines=st.integers(1, 500),
)


def oracle(rule, ctx, usage) -> bool:
    """Every condition written out again, independently of ``match_rule``."""
    wanted = ctx.signature if rule.match_type == "signature" else ctx.category
    return (not ctx.paused and rule.status == "active" and ctx.now < rule.expires_at
            and ctx.kind == "code_change" and ctx.service_id is not None and ctx.service_id == rule.service_id
            and wanted == rule.match_value and ctx.severity >= max(3, rule.most_urgent_severity)
            and usage.hour < rule.max_per_hour and usage.incident_opening < rule.max_per_incident
            and bool(ctx.files) and len(ctx.files) <= rule.max_files and ctx.file_problem is None
            and ctx.estimated_lines is not None and ctx.estimated_lines <= rule.max_lines
            and bool((ctx.verification or "").strip())
            and ctx.live_code_folder is not None and ctx.live_code_folder == rule.code_folder
            and not ctx.cooldown_active)


@SETTINGS
@given(rules, contexts, usages)
def test_property_1_match_iff_every_condition_holds(rule, ctx, usage):
    assert (match_rule(rule, ctx, usage) is None) == oracle(rule, ctx, usage)


@SETTINGS
@given(rules, contexts, usages, st.integers(0, 3), st.integers(0, 3), st.integers(0, 3), st.integers(0, 3))
def test_property_2_matching_is_monotone_in_the_limits(rule, ctx, usage, dh, di, df, dl):
    import dataclasses

    looser = dataclasses.replace(rule, max_per_hour=min(10, rule.max_per_hour + dh),
                                 max_per_incident=min(3, rule.max_per_incident + di),
                                 max_files=min(20, rule.max_files + df), max_lines=min(500, rule.max_lines + dl * 50))
    if match_rule(rule, ctx, usage) is None:
        assert match_rule(looser, ctx, usage) is None
    if match_rule(looser, ctx, usage) is not None:
        assert match_rule(rule, ctx, usage) is not None
    # a stricter urgency limit can only remove matches
    if rule.most_urgent_severity == 3:
        stricter = dataclasses.replace(rule, most_urgent_severity=4)
        if match_rule(stricter, ctx, usage) is None:
            assert match_rule(rule, ctx, usage) is None


@SETTINGS
@given(st.lists(rules, min_size=0, max_size=6), contexts, usages)
def test_property_3_first_match_wins(rule_list, ctx, usage):
    numbered = [__import__("dataclasses").replace(r, id=f"r{i + 1}", seq=i + 1) for i, r in enumerate(rule_list)]
    shuffled = list(reversed(numbered))
    winner, checks = first_match(shuffled, ctx, lambda rid: usage)
    expected = next((r for r in numbered if oracle(r, ctx, usage)), None)
    assert winner == expected
    if winner is not None:
        assert checks[-1].rule_id == winner.id and checks[-1].failed is None
        assert all(c.failed is not None for c in checks[:-1])
    else:
        assert len(checks) == len(numbered)


@SETTINGS
@given(rules, contexts, usages)
def test_property_4_never_covered(rule, ctx, usage):
    import dataclasses

    for broken in (dataclasses.replace(ctx, severity=min(ctx.severity, 2)),
                   dataclasses.replace(ctx, service_id=None),
                   dataclasses.replace(ctx, live_code_folder=None),
                   dataclasses.replace(ctx, paused=True)):
        assert match_rule(rule, broken, usage) is not None
