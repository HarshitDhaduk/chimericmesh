"""The README documents the dashboard (dashboard task 10.1; R1.2, R2.2, R4.5)."""
from __future__ import annotations
import re
from pathlib import Path
README = Path(__file__).resolve().parents[2] / "README.md"
def section() -> str:
    text = README.read_text(encoding="utf-8")
    assert len(re.findall(r"^## Dashboard$", text, re.M)) == 1, "one '## Dashboard' section"
    start = text.index("\n## Dashboard\n")
    nxt = re.search(r"^## ", text[start + 5:], re.M)
    return text[start: start + 5 + nxt.start()] if nxt else text[start:]
def test_names_the_four_views():
    s = section()
    for view in ("Overview", "Services", "Discovery", "Rules"):
        assert view in s, view
def test_token_handling_is_described():
    s = section()
    assert "once per browser session" in s
    assert "sessionStorage" in s and "not `localStorage`" in s
    assert re.search(r"pause, revoke and withdraw need no token", s, re.I)
def test_residual_risk_is_stated():
    s = section()
    assert "no Content-Security-Policy" in s
    assert "could read the token" in s
    assert "never renders log text as HTML" in s
def test_live_log_and_browser_limit():
    s = section()
    for piece in ("Server-Sent Events", "20 streams", "Last-Event-ID", "?tail=100", "about six connections per origin"):
        assert piece in s, piece
def test_scripted_stream_limit_check():
    s = section()
    assert "curl.exe" in s and "'-N'" in s and "429" in s and "Retry-After: 5" in s
    assert len(re.findall(r"1\.\.20", s)) == 1
def test_self_test_and_hostile_text_commands():
    s = section()
    assert "--headless=new" in s and "#selftest" in s and "SELFTEST PASS" in s
    assert "--dump-dom" in s and "&lt;img src=x" in s and "Start-Process" in s
    assert "<img src=x onerror=alert(1)>" in s and "<script>x</script>" in s
def test_nothing_starts_on_load():
    s = section()
    assert re.search(r"never starts a scan or an approval (when it loads|on load)", s)
def test_other_sections_are_unchanged_in_name():
    text = README.read_text(encoding="utf-8")
    for heading in ("## Quick start", "## Discovery", "## Standing approval rules", "## Access control",
                    "## The dashboard terminal and guided workflow"):
        assert heading in text, heading
