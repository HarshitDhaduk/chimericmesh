"""The README documents the rules honestly (task 8.3)."""
from __future__ import annotations

from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def section() -> str:
    start = README.index("## Standing approval rules")
    end = README.index("\n## ", start + 5)
    return README[start:end]


def test_the_section_exists_once():
    assert README.count("## Standing approval rules") == 1


def test_commands_variables_and_the_validity_window_are_named():
    text = section()
    for needle in ("!revoke", "!pause", "!rules", "!withdraw", "CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS",
                   "CHIMERICMESH_PROPOSAL_TTL_HOURS", "30 minutes", "AUTO-APPROVED", "rule-defaults"):
        assert needle in text, needle


def test_reserved_prefixes_are_listed():
    text = section()
    for prefix in ("`rule:`", "`rule-`", "`withdrawn:`", "`expired:`", "`expired-`"):
        assert prefix in text, prefix


def test_who_needs_the_token_is_stated_in_one_line():
    text = section()
    assert "pause, revoke and withdraw need no token" in text
    assert "create, widen, re-enable and resume need the token" in text


def test_the_honest_limits_are_stated():
    text = section()
    assert "same OS user" in text and "could read the token file" in text
    assert "Kiro's shell permission prompt is the real gate" in text
    assert "does not stop file writes outside the code folder" in text
