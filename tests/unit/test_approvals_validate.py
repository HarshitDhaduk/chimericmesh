from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import math

import pytest

from chimericmesh.approvals.errors import ProposalInvalid, RuleInvalid
from chimericmesh.approvals.models import MAX_EXPIRY_SECONDS
from chimericmesh.approvals.validate import (
    clean_note, validate_completion, validate_new_proposal, validate_rule_fields, validate_rule_patch,
)

NOW = 1_000_000.0
BASE = {"service_id": "api-gw", "match_type": "signature", "match_value": "disk full"}


def rule(**kw):
    return validate_rule_fields({**BASE, **kw}, now=NOW)


def bad_rule(field, **kw):
    with pytest.raises(RuleInvalid) as err:
        validate_rule_fields({**BASE, **kw}, now=NOW)
    assert err.value.field == field


def test_defaults():
    r = rule()
    assert (r.most_urgent_severity, r.max_per_hour, r.max_per_incident, r.max_files, r.max_lines) == (3, 3, 1, 5, 100)
    assert r.expires_at == NOW + 7 * 86400
    assert r.note == ""


@pytest.mark.parametrize("value", [2, 5, 7, True, 0, "3", 3.0])
def test_severity_bounds(value):
    bad_rule("most_urgent_severity", most_urgent_severity=value)


def test_severity_4_ok():
    assert rule(most_urgent_severity=4).most_urgent_severity == 4


@pytest.mark.parametrize("field,value", [
    ("max_per_hour", 0), ("max_per_hour", 11), ("max_per_incident", 0), ("max_per_incident", 4),
    ("max_files", 0), ("max_files", 21), ("max_lines", 0), ("max_lines", 501), ("max_lines", True),
])
def test_numeric_bounds(field, value):
    bad_rule(field, **{field: value})


@pytest.mark.parametrize("field,value", [
    ("max_per_hour", 10), ("max_per_incident", 3), ("max_files", 20), ("max_lines", 500),
    ("max_per_hour", 1), ("max_lines", 1),
])
def test_numeric_edges_accepted(field, value):
    assert getattr(rule(**{field: value}), field) == value


@pytest.mark.parametrize("value", [NOW - 1, NOW, NOW + MAX_EXPIRY_SECONDS + 1, math.nan, math.inf, -math.inf,
                                   True, "tomorrow"])
def test_expires_at_bounds(value):
    bad_rule("expires_at", expires_at=value)


def test_expires_at_max_ok():
    assert rule(expires_at=NOW + MAX_EXPIRY_SECONDS).expires_at == NOW + MAX_EXPIRY_SECONDS


@pytest.mark.parametrize("value", [0, 201])
def test_signature_length(value):
    bad_rule("match_value", match_value="x" * value if value else "")


def test_signature_control_char_and_blank():
    bad_rule("match_value", match_value="a\nb")
    bad_rule("match_value", match_value="   ")


def test_category_must_be_known():
    bad_rule("match_value", match_type="category", match_value="weather")
    assert rule(match_type="category", match_value="disk").match_value == "disk"


def test_match_type_and_service_slug():
    bad_rule("match_type", match_type="regex")
    bad_rule("service_id", service_id="Bad Id")
    bad_rule("service_id", service_id="unassigned")
    bad_rule("service_id", service_id=None)


def test_note_is_cut_and_cleaned():
    assert len(rule(note="x" * 500).note) == 200
    assert rule(note="a\r\nb\x00").note == "ab"


@pytest.mark.parametrize("key", ["created_by", "status", "id", "use_count", "code_folder", "surprise"])
def test_forbidden_and_unknown_keys_rejected(key):
    bad_rule(key, **{key: "x"})


def test_patch_accepts_known_fields_only():
    p = validate_rule_patch({"max_lines": 50, "note": "n"}, now=NOW, created_at=NOW - 10)
    assert p.max_lines == 50 and p.note == "n" and p.max_files is None
    for key in ("match_value", "service_id", "status", "created_by"):
        with pytest.raises(RuleInvalid):
            validate_rule_patch({key: "x"}, now=NOW, created_at=NOW)


def test_patch_expiry_is_relative_to_created_at():
    created = NOW - 10 * 86400
    ok = validate_rule_patch({"expires_at": created + MAX_EXPIRY_SECONDS}, now=NOW, created_at=created)
    assert ok.expires_at == created + MAX_EXPIRY_SECONDS
    with pytest.raises(RuleInvalid):
        validate_rule_patch({"expires_at": created + MAX_EXPIRY_SECONDS + 1}, now=NOW, created_at=created)
    with pytest.raises(RuleInvalid):
        validate_rule_patch({"expires_at": NOW - 1}, now=NOW, created_at=created)
    with pytest.raises(RuleInvalid):
        validate_rule_patch({"max_per_hour": 11}, now=NOW, created_at=created)


def prop(code_folder="/code", **kw):
    return validate_new_proposal(kw, code_folder=code_folder)


def bad_prop(field, code_folder="/code", **kw):
    with pytest.raises(ProposalInvalid) as err:
        validate_new_proposal(kw, code_folder=code_folder)
    assert err.value.field == field


def test_proposal_without_structure_is_accepted():
    assert prop() == {}


def test_proposal_normalized_fields():
    out = prop(kind="code_change", files=["a.py"], estimated_lines=5, verification="run tests")
    assert out == {"kind": "code_change", "files": ["a.py"], "estimated_lines": 5, "verification": "run tests"}


def test_proposal_bounds():
    bad_prop("files", files=["a.py"] * 21)
    bad_prop("files", files=["a" * 301])
    bad_prop("files", files="a.py")
    bad_prop("files", files=[1])
    bad_prop("estimated_lines", estimated_lines=1001)
    bad_prop("estimated_lines", estimated_lines=True)
    bad_prop("estimated_lines", estimated_lines=-1)
    bad_prop("verification", verification="v" * 301)
    bad_prop("kind", kind="config_change")
    bad_prop("surprise", surprise=1)
    assert prop(files=["a.py"] * 20, estimated_lines=1000)["estimated_lines"] == 1000


def test_files_with_a_folder_reject_absolute_dotdot_and_controls():
    for name in ["/etc/x.py", "C:\\x.py", "\\\\srv\\x.py", "../x.py", "a/../b.py", "a\\..\\b.py", "a\x00.py", "a\x1b.py"]:
        bad_prop("files", files=[name])


def test_files_without_a_folder_only_check_length_and_controls():
    assert prop(code_folder=None, files=["/etc/x.py", "../x.py"])["files"] == ["/etc/x.py", "../x.py"]
    bad_prop("files", code_folder=None, files=["a\x00"])
    bad_prop("files", code_folder=None, files=["a" * 301])


def test_verification_is_cleaned():
    assert prop(verification="run\ttests\n")["verification"] == "run tests"


def test_completion_bounds():
    ok = validate_completion({"files_changed": ["a.py"], "lines_changed": 3, "verification_result": "passed"})
    assert ok == {"files_changed": ["a.py"], "lines_changed": 3, "verification_result": "passed"}
    for raw, field in [({"files_changed": ["a"] * 51}, "files_changed"), ({"lines_changed": -1}, "lines_changed"),
                       ({"lines_changed": 1_000_001}, "lines_changed"), ({"lines_changed": True}, "lines_changed"),
                       ({"verification_result": "ok"}, "verification_result"),
                       ({"files_changed": ["a" * 301]}, "files_changed"), ({"files_changed": [3]}, "files_changed")]:
        with pytest.raises(ProposalInvalid) as err:
            validate_completion(raw)
        assert err.value.field == field
    assert validate_completion({}) == {}
    assert validate_completion(None) == {}


def test_clean_note():
    assert clean_note(None) == ""
    assert clean_note("a\x00\nb") == "ab"
    assert len(clean_note("x" * 999)) == 200
    assert clean_note(12345) == "12345"
