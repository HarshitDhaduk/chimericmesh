"""Text helpers and constants of the discovery module (R4.1, R4.7)."""
from __future__ import annotations

import pytest

from chimericmesh.discovery import models
from chimericmesh.discovery.text import (
    clean_text,
    normalize_process_name,
    os_name_from_platform,
    path_key,
)


def test_clean_text_removes_control_characters_and_surrogates():
    assert clean_text("a\x00b\x1bc\x7fd\x85e\ud800f") == "abcdef"
    assert clean_text("  x \n") == "x"


def test_clean_text_cuts_and_accepts_non_text():
    assert clean_text("x" * 5000) == "x" * 1024
    assert clean_text("abcdef", 3) == "abc"
    assert clean_text(12345) == "12345"
    assert clean_text(None) == "None"


@pytest.mark.parametrize("raw,expected", [
    ("/usr/sbin/nginx", "nginx"),
    ("C:\\Windows\\System32\\W3WP.EXE", "w3wp"),
    ("NGINX.EXE", "nginx"),
    ("httpd", "httpd"),
    ("a.exe.exe", "a"),
    ("", ""),
    ("\x00\x1b", ""),
    ("dir/", ""),
])
def test_normalize_process_name(raw, expected):
    assert normalize_process_name(raw, "windows") == expected


def test_normalize_process_name_cuts_to_64_and_is_idempotent():
    long = "A" * 200
    once = normalize_process_name(long, "linux")
    assert len(once) == 64
    assert normalize_process_name(once, "linux") == once
    tricky = "a" * 60 + ".exe" + "zzz"
    out = normalize_process_name(tricky, "linux")
    assert not out.endswith(".exe") and len(out) <= 64


def test_normalize_process_name_hostile_input_never_raises():
    for raw in (None, 5, b"x", object(), "\ud800", "\u0130" * 100):
        assert isinstance(normalize_process_name(raw, "linux"), str)


def test_path_key_windows_is_case_and_separator_insensitive():
    assert path_key("C:\\Logs\\A.log", "windows") == path_key("c:/logs/a.LOG", "windows")


def test_path_key_linux_is_case_sensitive():
    assert path_key("/var/log/A.log", "linux") != path_key("/var/log/a.log", "linux")
    assert path_key("/var/log/../log/a.log", "linux") == "/var/log/a.log"


def test_os_name_from_platform():
    assert os_name_from_platform("win32") == "windows"
    assert os_name_from_platform("cygwin") == "windows"
    assert os_name_from_platform("linux") == "linux"
    assert os_name_from_platform("linux2") == "linux"
    assert os_name_from_platform("darwin") == "macos"
    assert os_name_from_platform("freebsd13") is None


def test_constants_have_the_design_values():
    assert models.SCAN_TOTAL_SECONDS == 30.0
    assert models.TOOL_TIMEOUT_SECONDS == 10.0
    assert models.MAX_TOOL_OUTPUT_BYTES == 2 * 1024 * 1024
    assert models.MAX_NAMES_PER_PATTERN == 200
    assert models.MAX_SUGGESTIONS == 50
    assert models.MAX_PATHS_PER_SUGGESTION == 25
    assert models.MAX_PROCESS_NAME == 64
    assert models.SCAN_TTL_SECONDS == 3600.0
    assert models.MAX_KEPT_SCANS == 5
    assert models.MAX_DISMISSALS == 500
    assert models.PORT_ONLY_PORTS == frozenset({21, 23, 25, 110, 143, 161, 465, 514, 587, 993, 995, 3128})
