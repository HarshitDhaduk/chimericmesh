"""Permissions snippet (task 6.1): build_snippet, validate_test_command, find_workspace_root (R5.4 to R5.8)."""
from __future__ import annotations
import json
import logging
import os
import re
from types import SimpleNamespace
import pytest
from chimericmesh.agent.errors import InvalidQuery, NoCodeFolder
from chimericmesh.agent.snippet import build_snippet, find_workspace_root, validate_test_command
from chimericmesh.approvals.scope import PROTECTED_PATTERNS
def service(folder: str | None, sid: str = "legacy") -> SimpleNamespace:
    return SimpleNamespace(id=sid, code_folder=folder)
@pytest.fixture
def repo(tmp_path):
    (tmp_path / "examples" / "legacy_app").mkdir(parents=True)
    (tmp_path / ".kiro").mkdir()
    return tmp_path
def allow_lines(text: str) -> list[str]:
    """The JSON patterns of every uncommented fs_write allow rule."""
    out, cap, eff = [], None, None
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if "capability:" in line:
            cap = line.split(":", 1)[1].strip()
        elif "effect:" in line:
            eff = line.split(":", 1)[1].strip()
        elif "match:" in line and cap == "fs_write" and eff == "allow":
            out += json.loads(line.split("match:", 1)[1].strip())
    return out
def test_folder_inside_the_workspace_gets_a_relative_allow_and_protected_denies(repo):
    folder = str(repo / "examples" / "legacy_app")
    text = build_snippet(service(folder), workspace_root=str(repo))
    assert allow_lines(text) == ["examples/legacy_app/**"]
    assert "examples/legacy_app/**" in text and str(repo) not in allow_lines(text)[0]
    for pattern in PROTECTED_PATTERNS:
        assert json.dumps(pattern) in text
    deny = [l for l in text.splitlines() if l.strip().startswith("effect: deny")]
    assert deny
def test_allow_line_is_quoted_with_json_dumps_and_has_no_drive_or_leading_slash(repo):
    (repo / "a b #c").mkdir()
    text = build_snippet(service(str(repo / "a b #c")), workspace_root=str(repo))
    assert allow_lines(text) == ["a b #c/**"]
    assert 'match: ["a b #c/**"]' in text
    assert not re.search(r'"[A-Za-z]:|"[/\\]', "\n".join(allow_lines(text)))
@pytest.mark.parametrize("name", ["a*b", "a?b", "a[b", "a{b", "a]b", "a}b"])
def test_glob_characters_in_the_relative_path_give_no_allow_line(repo, name):
    try:
        (repo / name).mkdir()
    except OSError:
        pytest.skip("name not valid on this file system")
    text = build_snippet(service(str(repo / name)), workspace_root=str(repo))
    assert allow_lines(text) == []
    assert "Kiro prompts for each write" in text
def test_folder_outside_equal_to_or_unknown_workspace_give_no_allow_line(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    for folder, root in ((str(other), str(repo)), (str(repo), str(repo)), (str(repo / "examples"), None)):
        text = build_snippet(service(folder), workspace_root=root)
        assert allow_lines(text) == []
        assert "the fix still runs" in text.lower() or "the fix runs" in text.lower()
        assert "Kiro prompts for each write" in text
def test_a_sibling_with_the_same_prefix_is_outside(repo):
    sibling = repo.parent / (repo.name + "-evil")
    sibling.mkdir(exist_ok=True)
    assert allow_lines(build_snippet(service(str(sibling)), workspace_root=str(repo))) == []
def test_no_code_folder_raises():
    with pytest.raises(NoCodeFolder):
        build_snippet(service(None), workspace_root="/x")
def test_first_two_lines_name_the_mode_and_say_prompt_mode_is_default(repo):
    lines = build_snippet(service(str(repo / "examples")), workspace_root=str(repo)).splitlines()
    head = "\n".join(lines[:2])
    assert "Mode: AUTONOMOUS" in head and "Prompt mode is the default" in head
def test_scope_text_names_both_files_and_prefers_the_workspace_one(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    assert "~/.kiro/settings/permissions.yaml" in text and "every Kiro session and agent" in text
    assert "~/.kiro/workspace-roots/<hash>/permissions.yaml" in text and "preferred" in text
    assert "hash" in text and "look" in text
def test_trade_off_text_is_present(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    assert "no longer prompt" in text and "still pending" in text
    for part in ("prompt text", "verify_proposal", "audit trail"):
        assert part in text
def test_shell_examples_are_commented_and_the_default_command_is_python_m_pytest(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    assert '#    match: ["python -m pytest -q"]' in text
    custom = build_snippet(service(str(repo / "examples")), workspace_root=str(repo), test_command="pytest -x -q")
    assert '#    match: ["pytest -x -q"]' in custom and "python -m pytest -q" not in custom
    for line in text.splitlines():
        assert not (line.strip().startswith("- capability: shell") and not line.startswith("#"))
        assert "capability: shell" not in line or line.startswith("#")
def test_invalid_test_command_is_rejected_by_build_snippet(repo):
    with pytest.raises(InvalidQuery):
        build_snippet(service(str(repo / "examples")), workspace_root=str(repo), test_command="curl x")
@pytest.mark.parametrize("bad", ["*", "pytest *", "python -m pytest 'x'", 'pytest "x"', "a; b", "a & b", "a | b", "a$b",
                                 "a`b`", "python -c x", "python3 -c", "curl x", "wget x", "nc host", "powershell x",
                                 "/usr/bin/curl x", "pwsh", "bash x", "sh x", "cmd /c x", "", " ", "a" * 81,
                                 "a\nb", "a>b", "a<b", "(a)", "pytest -qc"])
def test_validate_test_command_rejects(bad):
    with pytest.raises(InvalidQuery):
        validate_test_command(bad)
@pytest.mark.parametrize("good", ["python -m pytest -q", "pytest -x -q", "python -m pytest tests/unit -q",
                                  "npm:test", "python -m unittest discover -s tests"])
def test_validate_test_command_accepts(good):
    assert validate_test_command(good) == good
def test_forbidden_words_never_appear(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    low = text.lower()
    for word in ("allow-all", "trust-all-tools", "curl", "wget", "python -c", "powershell", " nc "):
        assert word not in low, word
    assert not re.search(r'match:\s*\[\s*"\*"', text)
def test_active_proposal_file_allow_is_commented(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    assert '#    match: [".chimericmesh-active-proposal"]' in text
    assert ".chimericmesh-active-proposal" not in "\n".join(allow_lines(text))
def test_no_control_character_other_than_newline(repo):
    text = build_snippet(service(str(repo / "examples"), sid="x\x00y\r\nz\u2028"), workspace_root=str(repo) + "\x1b[1m\nX")
    assert not re.search("[\\x00-\\x09\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029]", text)
    assert text.count("Workspace root used:") == 1
def test_workspace_root_is_named(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    line = next(l for l in text.splitlines() if l.startswith("# Workspace root used:"))
    assert json.dumps(str(repo)) in line
    unknown = build_snippet(service(str(repo / "examples")), workspace_root=None)
    assert "# Workspace root used: (unknown)" in unknown
def test_unverified_schema_comment_and_relative_deny_caveat(repo):
    text = build_snippet(service(str(repo / "examples")), workspace_root=str(repo))
    assert "https://kiro.dev/docs/permissions/" in text and "verify before pasting" in text
    assert "relative deny" in text and "every workspace" in text
def test_check_live_note_is_present(repo):
    assert "check live" in build_snippet(service(str(repo / "examples")), workspace_root=str(repo)).lower()
# ---- find_workspace_root ------------------------------------------------------------------------
def exists_in(*dirs: str):
    known = {os.path.normpath(d) for d in dirs}
    return lambda p: os.path.normpath(p) in known
def test_a_valid_environment_value_wins():
    root = os.path.abspath("wsroot")
    got = find_workspace_root({"CHIMERICMESH_WORKSPACE_ROOT": root}, os.path.abspath("elsewhere"), exists=exists_in(root))
    assert got == root
def test_an_invalid_environment_value_is_not_used_and_one_warning_is_logged(caplog):
    cwd = os.path.abspath("repo")
    with caplog.at_level(logging.WARNING):
        got = find_workspace_root({"CHIMERICMESH_WORKSPACE_ROOT": "relative/nope"}, cwd,
                                  exists=exists_in(os.path.join(cwd, ".kiro")))
    assert got is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
def test_cwd_src_walks_up_to_the_folder_with_dot_kiro():
    repo = os.path.abspath("repo")
    got = find_workspace_root({}, os.path.join(repo, "src"), exists=exists_in(os.path.join(repo, ".kiro")))
    assert got == repo
def test_no_dot_kiro_anywhere_gives_the_working_directory():
    cwd = os.path.join(os.path.abspath("repo"), "src")
    assert find_workspace_root({}, cwd, exists=lambda p: False) == cwd
def test_demo_folder_gets_the_allow_line_with_the_walked_up_root(repo):
    root = find_workspace_root({}, str(repo / "src"), exists=lambda p: os.path.isdir(p) or p == str(repo / "src"))
    assert os.path.normcase(root) == os.path.normcase(str(repo))
    text = build_snippet(service(str(repo / "examples" / "legacy_app")), workspace_root=root)
    assert allow_lines(text) == ["examples/legacy_app/**"]
