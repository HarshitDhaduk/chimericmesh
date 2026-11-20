"""Task 8.4: the README states how log files and TCP syslog work, and their limits."""
from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
TITLE = "## Log files and TCP syslog"


def section() -> str:
    body = README.read_text(encoding="utf-8")
    start = body.index(TITLE)
    nxt = re.search(r"\n## ", body[start + len(TITLE):])
    s = body[start: start + len(TITLE) + (nxt.start() if nxt else len(body))]
    return re.sub(r"\s+", " ", s)


def test_section_exists_and_is_short():
    assert len(section()) < 4000


def test_registering_and_format_names():
    s = section()
    assert "/log-sources" in s
    for name in ("syslog-text", "ncsa", "w3c", "xferlog", "plain"):
        assert f"`{name}`" in s


def test_rotation_and_what_can_be_lost():
    s = section()
    assert "never holds it" in s and "rename" in s and "Windows" in s
    assert "last half second before a rename or truncation" in s
    assert "without its newline" in s


def test_offsets_crash_and_restart():
    s = section()
    assert "every 5 seconds" in s
    assert "repeat up to 5 seconds of lines" in s
    assert "few hundred milliseconds" in s


def test_rename_retry_with_measured_rate():
    s = section()
    assert "retry" in s and "WinError 32" in s
    assert re.search(r"\d(\.\d)? to \d percent", s)


def test_reenable_times_limits_paths_port_and_redaction():
    s = section()
    assert "disabled and enabled again starts at the end of the file" in s
    assert "arrival time" in s
    assert "2000 lines per second" in s and "8192 bytes per line" in s and "1 MiB per poll" in s
    assert "u_exYYMMDD.log" in s and "new path each day" in s
    assert "CHIMERICMESH_SYSLOG_TCP_PORT" in s and "5514" in s and "`0` disables it" in s
    assert "<redacted>" in s and "best effort" in s and "not a guarantee" in s
    for word in ("password=", "token=", "Authorization:", "query strings"):
        assert word in s
