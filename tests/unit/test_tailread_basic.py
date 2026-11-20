"""Task 4.1: ``poll_file`` state, identity, appends, partial lines and decoding."""
from __future__ import annotations

import os

from chimericmesh.connectors.tailread import RawLine, TailState, decode_line, header_fields, poll_file


def append(path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def datas(result) -> list[bytes]:
    return [x.data for x in result.lines]


def attach(path, **kw):
    return poll_file(str(path), TailState(), **kw)


def test_fresh_state_on_existing_file_emits_nothing_and_records_identity(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"one\ntwo\npartial")
    r = attach(p)
    assert r.status == "running" and r.lines == () and r.reason == "attached"
    assert r.state.ident is not None and r.state.offset == len(b"one\ntwo\n")
    assert r.state.ident.head_len == 15 and len(r.state.ident.head_sha) == 64


def test_appended_lines_are_emitted_in_order_with_end_offsets(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"old\n")
    s = attach(p).state
    append(p, b"first\nsecond\n")
    r = poll_file(str(p), s)
    assert datas(r) == [b"first", b"second"]
    assert [x.end for x in r.lines] == [4 + 6, 4 + 6 + 7]
    assert r.state.offset == p.stat().st_size


def test_unchanged_file_is_not_opened(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_bytes(b"old\n")
    s = attach(p).state
    append(p, b"x\n")
    s = poll_file(str(p), s).state
    calls = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: calls.append(a) or real(*a, **k))
    r = poll_file(str(p), s)
    assert r.status == "running" and r.lines == () and calls == []


def test_crlf_stripped_and_empty_line_is_a_line(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"a\r\n\r\nb\n")
    r = poll_file(str(p), s)
    assert datas(r) == [b"a", b"", b"b"]


def test_partial_line_waits_for_newline_and_is_read_again(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"a\n")
    s = attach(p).state
    append(p, b"par")
    r1 = poll_file(str(p), s)
    assert r1.lines == () and r1.state.offset == 2
    assert r1.bytes_read == 5 + 3  # head (whole 5-byte file) + the unfinished bytes
    append(p, b"t\n")
    r2 = poll_file(str(p), r1.state)
    assert datas(r2) == [b"part"]
    assert r2.bytes_read == 7 + 5


def test_unfinished_overlong_lines_are_cut_once_and_the_rest_skipped(tmp_path):
    for size in (8192, 20000):
        p = tmp_path / f"o{size}.log"
        p.write_bytes(b"x\n")
        s = attach(p).state
        append(p, b"y" * size)
        r1 = poll_file(str(p), s)
        assert len(r1.lines) == 1
        line = r1.lines[0]
        assert len(line.data) == 8192 and line.truncated and line.discarding_after
        assert r1.state.discarding and r1.state.offset == 2 + size
        append(p, b"more\nnext\n")
        r2 = poll_file(str(p), r1.state)
        assert datas(r2) == [b"next"] and not r2.state.discarding
        r3 = poll_file(str(p), r2.state)
        assert r3.lines == ()


def test_restart_from_state_at_overlong_line_end_still_skips_the_rest(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"y" * 9000)
    r1 = poll_file(str(p), s)
    raw = r1.lines[0]
    assert raw.discarding_after
    restart = TailState(ident=r1.state.ident, offset=raw.end, discarding=raw.discarding_after)
    append(p, b"tail of the long line\nfresh\n")
    r2 = poll_file(str(p), restart)
    assert datas(r2) == [b"fresh"]


def test_complete_overlong_line_is_cut_and_flagged(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x\n")
    s = attach(p).state
    append(p, b"z" * 9000 + b"\nok\n")
    r = poll_file(str(p), s)
    assert [len(x.data) for x in r.lines] == [8192, 2]
    assert r.lines[0].truncated and not r.lines[1].truncated
    assert r.lines[0].end == 2 + 9001


def test_max_read_limits_one_poll_and_continues_exactly(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"start\n")
    s = attach(p).state
    payload = [b"line %04d " % i + b"p" * 30 for i in range(300)]
    append(p, b"\n".join(payload) + b"\n")
    got: list[bytes] = []
    polls = 0
    while True:
        r = poll_file(str(p), s, max_read=4096)
        polls += 1
        assert r.bytes_read <= 4096 + 256
        got += datas(r)
        s = r.state
        if not r.more:
            break
    assert polls > 1 and got == payload
    assert poll_file(str(p), s, max_read=4096).lines == ()


def test_decode_line():
    assert decode_line(RawLine(b"ok \xff\xfe bad", 0, False)) == "ok \ufffd\ufffd bad"
    assert decode_line(RawLine(b"a\x00b\x07c\td\x7f", 0, False)) == "abc\td"
    assert decode_line(RawLine(b"cut", 0, True)) == "cut [truncated]"


def test_head_fingerprint_upgrades_when_file_grows_from_empty(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"")
    r0 = attach(p)
    assert r0.state.ident is not None and r0.state.ident.head_len == 0
    append(p, b"h" * 300 + b"\n")
    r1 = poll_file(str(p), r0.state)
    assert r1.state.ident.head_len == 256
    assert datas(r1) == [b"h" * 300]
    assert r1.reason == ""


def test_poll_never_raises_on_bad_paths(tmp_path, monkeypatch):
    for bad in ("a\x00b", str(tmp_path / "x\x00y")):
        r = poll_file(bad, TailState())
        assert r.status == "error" and r.error == "invalid path" and r.lines == ()

    def unencodable(*a, **k):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(os, "stat", unencodable)
    r = poll_file("whatever", TailState())
    assert r.status == "error" and r.error == "invalid path"


def test_header_fields(tmp_path):
    p = tmp_path / "w.log"
    p.write_bytes(b"#Software: IIS\n#Fields: a b\n#Fields: date time cs-method\n1 2 GET\n")
    assert header_fields(str(p)) == "#Fields: date time cs-method"
    p.write_bytes(b"#Fields: a b\n" + b"x" * 5000 + b"\n#Fields: late\n")
    assert header_fields(str(p)) == "#Fields: a b"
    p.write_bytes(b"x" * 4090 + b"\n#Fields: beyond\n")
    assert header_fields(str(p)) is None
    p.write_bytes(b"no header\n")
    assert header_fields(str(p)) is None
    assert header_fields(str(tmp_path / "missing")) is None
    assert header_fields(str(tmp_path)) is None
    assert header_fields("a\x00b") is None
