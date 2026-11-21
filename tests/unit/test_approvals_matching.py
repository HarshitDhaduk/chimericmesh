"""Rule matching. The requirement's conditions (a) to (m) map in order to 4.2.1 to 4.2.13 (design L8).

Each test starts from a rule and context that match and changes exactly one thing.
"""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import inspect

from approvals_rules import NOW, make_ctx, make_rule, make_usage, replace

from chimericmesh.approvals import matching
from chimericmesh.approvals.matching import RULE_CONDITIONS, Condition, first_match, match_rule


def test_baseline_matches():
    assert match_rule(make_rule(), make_ctx(), make_usage()) is None


def test_condition_codes_are_in_evaluation_order():
    assert [c.value for c in Condition] == [
        "paused", "inactive", "kind", "service", "match", "severity", "hour_quota", "incident_quota", "files",
        "lines", "verification", "code_folder", "cooldown"]
    assert len(RULE_CONDITIONS) == 13


def test_4_2_1_paused():
    assert match_rule(make_rule(), make_ctx(paused=True), make_usage()) == "paused"


def test_4_2_2_inactive_status_and_expiry():
    for status in ("suspended", "revoked", "expired"):
        assert match_rule(make_rule(status=status), make_ctx(), make_usage()) == "inactive"
    rule = make_rule(expires_at=NOW)
    assert match_rule(rule, make_ctx(now=NOW), make_usage()) == "inactive"
    assert match_rule(rule, make_ctx(now=NOW - 1), make_usage()) is None


def test_4_2_3_kind():
    assert match_rule(make_rule(), make_ctx(kind="other"), make_usage()) == "kind"
    assert match_rule(make_rule(), make_ctx(kind=None), make_usage()) == "kind"


def test_4_2_4_service():
    assert match_rule(make_rule(), make_ctx(service_id=None), make_usage()) == "service"
    assert match_rule(make_rule(), make_ctx(service_id="other"), make_usage()) == "service"


def test_4_2_5_match_signature_and_category():
    assert match_rule(make_rule(), make_ctx(signature="disk other"), make_usage()) == "match"
    assert match_rule(make_rule(), make_ctx(signature="DISK FULL"), make_usage()) == "match"
    cat = make_rule(match_type="category", match_value="disk")
    assert match_rule(cat, make_ctx(category="disk", signature="anything"), make_usage()) is None
    assert match_rule(cat, make_ctx(category="memory"), make_usage()) == "match"


def test_4_2_6_severity():
    assert match_rule(make_rule(), make_ctx(severity=2), make_usage()) == "severity"
    assert match_rule(make_rule(most_urgent_severity=4), make_ctx(severity=3), make_usage()) == "severity"
    assert match_rule(make_rule(most_urgent_severity=3), make_ctx(severity=4), make_usage()) is None
    for sev in (0, 1):
        assert match_rule(make_rule(), make_ctx(severity=sev), make_usage()) == "severity"


def test_4_2_7_hour_quota():
    rule = make_rule(max_per_hour=3)
    assert match_rule(rule, make_ctx(), make_usage(hour=2)) is None
    assert match_rule(rule, make_ctx(), make_usage(hour=3)) == "hour_quota"


def test_4_2_8_incident_quota():
    rule = make_rule(max_per_incident=1)
    assert match_rule(rule, make_ctx(), make_usage(incident_opening=0)) is None
    assert match_rule(rule, make_ctx(), make_usage(incident_opening=1)) == "incident_quota"


def test_4_2_9_files():
    rule = make_rule(max_files=2)
    assert match_rule(rule, make_ctx(files=None), make_usage()) == "files"
    assert match_rule(rule, make_ctx(files=()), make_usage()) == "files"
    assert match_rule(rule, make_ctx(files=("a.py", "b.py", "c.py")), make_usage()) == "files"
    assert match_rule(rule, make_ctx(files=("a.py", "b.py")), make_usage()) is None
    assert match_rule(rule, make_ctx(file_problem="outside_folder"), make_usage()) == "files"


def test_4_2_10_lines():
    rule = make_rule(max_lines=100)
    assert match_rule(rule, make_ctx(estimated_lines=None), make_usage()) == "lines"
    assert match_rule(rule, make_ctx(estimated_lines=101), make_usage()) == "lines"
    assert match_rule(rule, make_ctx(estimated_lines=100), make_usage()) is None
    assert match_rule(rule, make_ctx(estimated_lines=0), make_usage()) is None


def test_4_2_11_verification():
    assert match_rule(make_rule(), make_ctx(verification=""), make_usage()) == "verification"
    assert match_rule(make_rule(), make_ctx(verification=None), make_usage()) == "verification"
    assert match_rule(make_rule(), make_ctx(verification="  "), make_usage()) == "verification"


def test_4_2_12_code_folder():
    assert match_rule(make_rule(), make_ctx(live_code_folder="/elsewhere"), make_usage()) == "code_folder"
    assert match_rule(make_rule(), make_ctx(live_code_folder=None), make_usage()) == "code_folder"


def test_4_2_13_cooldown():
    assert match_rule(make_rule(), make_ctx(cooldown_active=True), make_usage()) == "cooldown"


def test_first_failing_condition_is_returned():
    ctx = make_ctx(paused=True, kind=None, files=None)
    assert match_rule(make_rule(), ctx, make_usage()) == "paused"
    assert match_rule(make_rule(), replace(ctx, paused=False), make_usage()) == "kind"


def test_first_match_lowest_seq_wins_and_lists_checks():
    r1 = make_rule(id="r1", seq=1, match_value="other")
    r2 = make_rule(id="r2", seq=2)
    r3 = make_rule(id="r3", seq=3)
    rule, checks = first_match([r3, r2, r1], make_ctx(), lambda rid: make_usage())
    assert rule is r2
    assert [(c.rule_id, c.failed) for c in checks] == [("r1", "match"), ("r2", None)]


def test_first_match_with_no_rules_or_no_match():
    assert first_match([], make_ctx(), lambda rid: make_usage()) == (None, ())
    rule, checks = first_match([make_rule()], make_ctx(paused=True), lambda rid: make_usage())
    assert rule is None and [(c.rule_id, c.failed) for c in checks] == [("r1", "paused")]


def test_match_rule_never_raises_and_reads_no_clock():
    src = inspect.getsource(matching)
    assert "time.time" not in src and "datetime" not in src
    assert match_rule(make_rule(), make_ctx(files=(None,)), make_usage()) in (None, "files", "error")
