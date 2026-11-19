"""Task 3.1: the TCP syslog frame decoder."""
from __future__ import annotations

import pytest

from chimericmesh.connectors.framing import FrameDecoder, FrameError, detect_mode


@pytest.mark.parametrize(
    "buf,mode",
    [
        (b"12 <34>x", "octet"),
        (b"1234 x", "octet"),
        (b"12345 x", "lf"),
        (b"102400 x", "lf"),
        (b"2026-11-18T10:00:00 host x", "lf"),
        (b"<34>Nov 18", "lf"),
        (b"5-abc", "lf"),
        (b"abc", "lf"),
        (b"\n", "lf"),
        (b"12", None),
        (b"", None),
        (b"1234", None),
        (b"12345", "lf"),
    ],
)
def test_detect_mode(buf, mode):
    assert detect_mode(buf) == mode


def test_octet_basic_and_two_frames():
    d = FrameDecoder()
    assert d.feed(b"5 hello") == [b"hello"]
    assert d.mode == "octet"
    d = FrameDecoder()
    assert d.feed(b"5 hello3 abc") == [b"hello", b"abc"]


def test_octet_one_byte_at_a_time():
    d = FrameDecoder()
    out: list = []
    for b in b"5 hello3 abc":
        out += d.feed(bytes([b]))
    assert out == [b"hello", b"abc"]


def test_octet_tolerates_crlf_between_frames():
    d = FrameDecoder()
    assert d.feed(b"5 hello\r\n3 abc\n") == [b"hello", b"abc"]


def test_frames_are_bytes():
    out = FrameDecoder().feed(b"5 hello")
    assert all(isinstance(f, bytes) for f in out)


def test_octet_exact_max_frame_accepted():
    d = FrameDecoder()
    payload = b"a" * 8192
    assert d.feed(b"8192 " + payload) == [payload]


@pytest.mark.parametrize(
    "first,data,reason",
    [
        (None, b"0 x", "zero_length"),
        (None, b"9999 x", "too_long"),
        (None, b"8193 x", "too_long"),
        (b"5 hello", b"12345 x", "bad_length"),
        (b"5 hello", b"102400 x", "bad_length"),
        (b"5 hello", b"x5 hello", "bad_length"),
        (b"5 hello", b"5hello", "bad_length"),
    ],
)
def test_octet_errors(first, data, reason):
    d = FrameDecoder()
    if first is not None:
        assert d.feed(first) == [b"hello"]
    out = d.feed(data)
    errors = [x for x in out if isinstance(x, FrameError)]
    assert len(errors) == 1 and errors[0].reason == reason
    assert out[-1] is errors[0]
    assert d.failed
    assert d.feed(b"5 hello") == []
    assert d.finish() == []


def test_frames_before_error_are_yielded_first():
    d = FrameDecoder()
    out = d.feed(b"5 hello0 ")
    assert out[0] == b"hello" and isinstance(out[1], FrameError) and len(out) == 2


def test_lf_splits_strips_cr_skips_empty():
    d = FrameDecoder()
    assert d.feed(b"abc\r\n\r\n\ndef\nghi") == [b"abc", b"def"]
    assert d.mode == "lf"
    assert d.finish() == [b"ghi"]


def test_lf_overlong_line_cut_and_rest_discarded():
    d = FrameDecoder()
    out = d.feed(b"x" * 8192)
    assert out == [b"x" * 8192]
    assert d.feed(b"yyyy" * 100) == []
    assert d.feed(b"zz\nnext\n") == [b"next"]
    assert d.finish() == []


def test_lf_overlong_line_with_newline_in_same_feed():
    d = FrameDecoder()
    out = d.feed(b"x" * 9000 + b"\nnext\n")
    assert out == [b"x" * 8192, b"next"]


def test_finish_octet_returns_nothing_and_lf_returns_tail():
    d = FrameDecoder()
    d.feed(b"5 hel")
    assert d.finish() == []
    d = FrameDecoder()
    assert d.feed(b"2026-11-18 x") == []
    assert d.finish() == [b"2026-11-18 x"]


def test_finish_undecided_digits_is_lf():
    d = FrameDecoder()
    assert d.feed(b"12") == []
    assert d.mode is None
    assert d.finish() == [b"12"]


def test_big_garbage_in_lf_mode_is_bounded():
    d = FrameDecoder()
    out = d.feed(b"abc\n" + (b"\xff\x00garbage" * 600000))
    assert out[0] == b"abc"
    assert all(len(x) <= 8192 for x in out)


def test_mode_fixed_after_decision():
    d = FrameDecoder()
    d.feed(b"hello\n")
    assert d.mode == "lf"
    assert d.feed(b"5 hello\n") == [b"5 hello"]
