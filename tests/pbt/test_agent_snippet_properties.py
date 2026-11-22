"""Property 10 of agent-loop: the permissions snippet is safe for any folder, workspace and service id."""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from hypothesis import assume, given, settings
from hypothesis import strategies as st
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from chimericmesh.agent.snippet import build_snippet  # noqa: E402
FORBIDDEN = ("allow-all", "trust-all-tools", "curl", "wget", "python -c", "powershell", " nc ")
GLOBS = set("*?[]{}")
component = st.text(alphabet="abcXYZ019 #'\"*?[]{}-_()", max_size=12).map(lambda t: "x" + t + "x")
roots = st.one_of(
    st.lists(component, min_size=1, max_size=3).map(lambda cs: os.path.join(os.path.abspath(os.sep), *cs)),
    st.lists(component, min_size=1, max_size=2).map(lambda cs: "C:\\" + "\\".join(cs)))
CONTROL = re.compile("[\\x00-\\x09\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029]")
def allow_patterns(text: str) -> list[str]:
    out, cap, eff = [], None, None
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert line.startswith(("rules:", "  ")), repr(line)           # nothing escapes the rules block
        if "capability:" in line:
            cap = line.split(":", 1)[1].strip()
        elif "effect:" in line:
            eff = line.split(":", 1)[1].strip()
        elif "match:" in line and cap == "fs_write" and eff == "allow":
            out += json.loads(line.split("match:", 1)[1].strip())
    return out
def checks(text: str) -> None:
    assert not CONTROL.search(text)
    assert "Mode: AUTONOMOUS" in "\n".join(text.splitlines()[:2])
    low = text.lower()
    for word in FORBIDDEN:
        assert word not in low, word
    assert not re.search(r'match:\s*\[\s*"\*"', text)
@settings(max_examples=100, deadline=None)
@given(roots, st.lists(component, max_size=3), st.sampled_from(["inside", "equal", "other", "unknown", "prefix"]),
       st.text(max_size=40))
def test_property_10_snippet_safety_for_built_paths(root, comps, kind, sid):
    for text in (root, sid, *comps):
        assume(not any(w in text.lower() for w in FORBIDDEN))
    sep = "\\" if root.startswith("C:") else os.sep
    if kind == "inside":
        assume(comps)
        folder = root + sep + sep.join(comps)
    elif kind == "equal":
        folder = root
    elif kind == "prefix":
        folder = root + "-evil" + sep + "x"
    else:
        folder = os.path.join(os.path.abspath(os.sep), "elsewhere", "x") if not root.startswith("C:") else "D:\\elsewhere\\x"
    ws = None if kind == "unknown" else root
    text = build_snippet(SimpleNamespace(id=sid, code_folder=folder), workspace_root=ws)
    checks(text)
    patterns = allow_patterns(text)
    rel = "/".join(comps)
    expect = kind == "inside" and not (GLOBS & set(rel))
    if root.startswith("C:") and os.name != "nt":
        return                       # a drive path is not a path of this host: only the safety checks apply
    assert (len(patterns) == 1) is expect, (patterns, folder, ws)
    if patterns:
        pattern = patterns[0]
        assert pattern == rel + "/**" and pattern.endswith("/**")
        assert not pattern.startswith(("/", "\\")) and not re.match(r"^[A-Za-z]:", pattern)
        assert not (GLOBS & set(pattern[:-3]))
@settings(max_examples=100, deadline=None)
@given(st.text(max_size=80), st.text(max_size=80), st.one_of(st.none(), st.text(max_size=80)))
def test_property_10_arbitrary_text_cannot_inject_a_rule_or_a_control_character(sid, folder, root):
    assume(folder)
    for text in (sid, folder, root or ""):
        assume(not any(w in text.lower() for w in FORBIDDEN))
    text = build_snippet(SimpleNamespace(id=sid, code_folder=folder), workspace_root=root)
    checks(text)
    patterns = allow_patterns(text)
    assert len(patterns) <= 1
    for pattern in patterns:
        assert pattern.endswith("/**") and not pattern.startswith(("/", "\\", "~"))
        assert not (GLOBS & set(pattern[:-3])) and ".." not in pattern.split("/")
