"""Properties 5, 15 and 20 of auto-approval: rule bounds, file scope containment, IRC line safety."""
from __future__ import annotations

import math
import os
import re
import tempfile

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.approvals.errors import RuleInvalid
from chimericmesh.approvals.format import auto_approved_line, cooldown_note, rule_line
from chimericmesh.approvals.scope import PROTECTED_PATTERNS, SOURCE_SUFFIXES, check_file, glob_to_regex
from chimericmesh.approvals.validate import validate_rule_fields, validate_rule_patch

NOW = 1_700_000_000.0
SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

weird_text = st.text(alphabet=st.characters(codec=None, blacklist_categories=()), max_size=400)
any_value = st.one_of(st.none(), st.booleans(), st.integers(min_value=-10**12, max_value=10**12),
                      st.floats(allow_nan=True, allow_infinity=True), weird_text,
                      st.lists(st.integers(), max_size=3))


# ---- Property 5: rule bounds ----------------------------------------------------------------------
@SETTINGS
@given(sev=any_value, hour=any_value, inc=any_value, files=any_value, lines=any_value, exp=any_value,
       value=any_value, mtype=st.sampled_from(["signature", "category", "x"]), note=any_value)
def test_property_5_rule_bounds(sev, hour, inc, files, lines, exp, value, mtype, note):
    raw = {"service_id": "svc", "match_type": mtype, "match_value": value, "most_urgent_severity": sev,
           "max_per_hour": hour, "max_per_incident": inc, "max_files": files, "max_lines": lines,
           "expires_at": exp, "note": note}
    raw = {k: v for k, v in raw.items() if v is not None or k in ("match_value",)}
    try:
        fields = validate_rule_fields(raw, now=NOW)
    except RuleInvalid:
        return
    assert fields.most_urgent_severity in (3, 4)
    assert 1 <= fields.max_per_hour <= 10
    assert 1 <= fields.max_per_incident <= 3
    assert 1 <= fields.max_files <= 20
    assert 1 <= fields.max_lines <= 500
    assert fields.expires_at is not None and math.isfinite(fields.expires_at)
    assert NOW < fields.expires_at <= NOW + 30 * 86400
    assert len(fields.note) <= 200 and not re.search(r"[\x00-\x1f]", fields.note)
    assert 1 <= len(fields.match_value) <= 200


@SETTINGS
@given(st.dictionaries(st.sampled_from(["most_urgent_severity", "max_per_hour", "max_per_incident", "max_files",
                                        "max_lines", "expires_at", "note", "status", "match_value"]),
                       any_value, max_size=6),
       st.floats(min_value=0, max_value=NOW, allow_nan=False))
def test_property_5_patch_bounds(raw, created_at):
    try:
        patch = validate_rule_patch(raw, now=NOW, created_at=created_at)
    except RuleInvalid:
        return
    if patch.max_per_hour is not None:
        assert 1 <= patch.max_per_hour <= 10
    if patch.expires_at is not None:
        assert NOW < patch.expires_at <= created_at + 30 * 86400
    if patch.most_urgent_severity is not None:
        assert patch.most_urgent_severity in (3, 4)


# ---- Property 15: file scope containment ------------------------------------------------------------
_TMP = tempfile.TemporaryDirectory()
FOLDER = os.path.realpath(os.path.join(_TMP.name, "app"))
os.makedirs(FOLDER, exist_ok=True)
_PROTECTED = [glob_to_regex(p) for p in PROTECTED_PATTERNS]

path_part = st.sampled_from(["a", "b.py", "..", ".", ".git", ".env", "x.PHP", "k.pem", "dir", "id_rsa", "c.json",
                             "", "SRC", "..\\", "a.py "])
path_text = st.one_of(st.lists(path_part, max_size=5).map("/".join), weird_text)


@SETTINGS
@given(path_text)
def test_property_15_accepted_files_are_contained_source_files(name):
    problem = check_file(FOLDER, name)
    if problem is not None:
        return
    joined = name if os.path.isabs(name) else os.path.join(FOLDER, name)
    real = os.path.normcase(os.path.realpath(joined))
    base = os.path.normcase(FOLDER)
    assert real != base and os.path.commonpath([real, base]) == base
    assert os.path.splitext(real)[1].lower() in SOURCE_SUFFIXES
    rel = os.path.relpath(real, base).replace(os.sep, "/")
    assert not any(rx.match(rel) for rx in _PROTECTED)
    assert len(name) <= 300 and not re.search(r"[\x00-\x1f\x7f-\x9f]", name)


@SETTINGS
@given(weird_text)
def test_property_15_never_raises(name):
    check_file(FOLDER, name)
    check_file("", name)


# ---- Property 20: IRC line safety --------------------------------------------------------------------
@SETTINGS
@given(pid=weird_text, rid=weird_text, sid=weird_text, action=st.text(max_size=5000), actor=weird_text,
       extra=weird_text)
def test_property_20_lines_are_one_safe_line(pid, rid, sid, action, actor, extra):
    lines = [auto_approved_line(pid, rid, sid, action), cooldown_note(pid, rid)]
    for verb in ("created", "revoked", "rule_paused", "enabled", "suspended", "expired", "paused", "resumed",
                 "withdrawn", "denied", "other"):
        lines.append(rule_line(verb, rid, sid, actor, extra))
    for line in lines:
        assert not re.search(r"[\r\n\x00]", line)
        assert len(line.encode("utf-8")) <= 400
        line.encode("utf-8")  # no lone surrogates
