"""The README Discovery section (task 8.3; findings 1 and 12; O11, O13)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("## Discovery")
    end = text.find("\n## ", start + 5)
    return text[start:end if end != -1 else None]


def test_readme_has_a_discovery_section():
    assert "## Discovery" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_opt_in_token_tools_privacy_and_limited():
    s = section()
    low = s.lower()
    assert "opt-in" in low and "admin token" in low
    for tool in ("tasklist", "netstat", "ss", "ps", "lsof", "/proc"):
        assert tool in s, tool
    assert "never reads log contents" in low or "never reads the contents" in low
    assert "no network connection" in low
    assert "`limited`" in s
    assert "one hour" in low
    assert "mcp tool" in low and "agent" in low


def test_dashboard_manual_path_daily_files_and_iis():
    s = section()
    low = s.lower()
    assert "dashboard" in low and "by hand" in low
    assert "files that exist at accept time" in low
    assert "a rotating log needs its new file added by hand; patterns are not stored" in low
    assert "iis" in low and "w3wp" in low and "first request" in low


def test_catalog_file_and_contingency_rule():
    s = section()
    assert "catalog/legacy_apps.json" in s
    assert "w3c" in s and "xferlog" in s and "`plain`" in s


def test_routes_and_manual_checks_are_listed():
    s = section()
    for route in ("POST /api/discovery/scan", "GET /api/discovery/scans/{id}",
                  "POST /api/discovery/suggestions/{id}/accept", "POST /api/discovery/suggestions/{id}/dismiss",
                  "GET /api/discovery/dismissals", "DELETE /api/discovery/dismissals"):
        assert route in s, route
    assert "Manual checks" in s
    assert "upstream.py" in s and "code_folder" in s
