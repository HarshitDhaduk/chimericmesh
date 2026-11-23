"""The README section "Agent watch mode" (agent-loop task 11.1). Phrase checks on whitespace-normalised text."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[2] / "README.md"


def section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Agent watch mode")
    end = text.index("\n## ", start + 5)
    return " ".join(text[start:end].split())


@pytest.fixture(scope="module")
def body() -> str:
    return section()


def test_names_profile_tool_modes_and_snippet_route(body):
    for phrase in ("mesh-watcher", "wait_for_work", "Prompt mode (default)", "Autonomous (hands-off) mode",
                   "/api/services/<id>/permissions-snippet", "verify_proposal", "`p1`, `p2`"):
        assert phrase in body, phrase


def test_states_the_trust_static_scope_and_glob_facts(body):
    for phrase in ("trust the workspace", "Permission rules are static", "Kiro prompts again",
                   "applies to every session and agent of this user", "workspace-scoped file",
                   "workspace-relative globs only", "`PermissionRequest` hook trigger is not part of V1"):
        assert phrase in body, phrase


def test_standing_allow_and_design_goal_wording(body):
    assert "standing allow" in body and "still `pending`" in body
    assert "design goal" in body and "not a guarantee" in body
    assert "token file" in body and "Kiro's own prompt on shell commands is the real gate" in body


def test_hands_off_test_command_risk_paragraph(body):
    for phrase in ("runs code the agent has just written, with your privileges", "read the admin token file",
                   "call the hub", "hands-off mode only", "prompt mode stays the default",
                   "Pause, revoke and withdraw need no token and can only reduce"):
        assert phrase in body, phrase


def test_wait_result_requesttimeout_and_fallback(body):
    for phrase in ("25 seconds", "35 seconds", "120000", "requestTimeout", ".kiro/settings/mcp.json",
                   "mcpServers.chimericmesh.requestTimeout", "was **not** checked", "CHIMERICMESH_WATCH_MAX_WAIT",
                   "environment of the hub process", "Only the hub reads it", "2026-11-23"):
        assert phrase in body, phrase


def test_check_live_paragraph_and_hook_limit(body):
    assert "check live" in body and "~/.chimericmesh/**" in body and "name-based denies" in body
    assert "the hook covers writes only" in body


def test_demo_precondition_and_proposal_list_limits(body):
    assert "git checkout -- examples/legacy_app/upstream.py" in body and "Nothing in this module runs that command" in body
    assert "keeps the latest 200" in body and "With the store, every proposal is kept" in body
    assert "protected names inside the code folder" in body


def test_workspace_root_and_absolute_files(body):
    for phrase in ("walking up", ".kiro/", "CHIMERICMESH_WORKSPACE_ROOT", "`files` in `verify_proposal` (and in the hook) are absolute paths",
                   "`files` in `propose_remediation` are written relative to the service's code folder"):
        assert phrase in body, phrase


def test_hook_install_effect_and_removal(body):
    for phrase in (".kiro/hooks-optional/", "copy both files into `.kiro/hooks/`", "delete both copies",
                   "every write needs an active proposal", ".chimericmesh-active-proposal", "exits 0",
                   "`.kiro/hooks/` is unchanged"):
        assert phrase in body, phrase


def test_no_other_section_lost():
    text = README.read_text(encoding="utf-8")
    for heading in ("## Standing approval rules", "## Kiro agent and power", "## Security notes", "## License"):
        assert heading in text
    assert len(re.findall(r"^## Agent watch mode$", text, re.M)) == 1