"""Task 1.5: the ``xferlog`` format (vsftpd, wu-ftpd)."""
from __future__ import annotations

import dataclasses

import pytest

from chimericmesh.parsers.logctx import FormatContext
from chimericmesh.parsers.plain import parse_plain
from chimericmesh.parsers.xferlog import parse_xferlog

CTX = FormatContext(machine="box", service_id="ftp", app="xferlog")


def rec(name: str = "/pub/a b.bin", direction: str = "o", status: str = "c") -> str:
    return f"Thu Nov 18 09:15:17 2026 1 192.0.2.5 1024 {name} b _ {direction} r alice ftp 0 * {status}"


def test_standard_line_with_space_in_file_name():
    out = parse_xferlog(rec(), CTX)
    assert out.matched
    ev = out.event
    assert ev.message == "download /pub/a b.bin (complete)" and ev.severity == 6
    assert ev.host == "box/ftp" and ev.app == "xferlog" and ev.source == "file"
    assert "192.0.2.5" in ev.raw and "192.0.2.5" not in ev.host + ev.message


def test_incomplete_transfer_is_severity_4():
    ev = parse_xferlog(rec(status="i"), CTX).event
    assert ev.severity == 4 and ev.message.endswith("(incomplete)")


@pytest.mark.parametrize("direction,word", [("o", "download"), ("i", "upload"), ("d", "delete")])
def test_directions(direction, word):
    assert parse_xferlog(rec(direction=direction), CTX).event.message.startswith(word + " ")


@pytest.mark.parametrize("text", ["Thu Nov 18 09:15:17 2026 1 192.0.2.5", "garbage", rec()[:-4]])
def test_garbled_line_is_plain_and_unmatched(text):
    out = parse_xferlog(text, CTX)
    expected = parse_plain(text, CTX).event
    assert not out.matched
    assert dataclasses.replace(out.event, ts=0) == dataclasses.replace(expected, ts=0)


def test_blank_line_is_skipped():
    assert parse_xferlog("", CTX).event is None
