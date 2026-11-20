"""Task 4.3: first attach, backfill and resume decisions."""
from __future__ import annotations

from chimericmesh.connectors.tailread import FileIdent, TailState, poll_file


def append(path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def datas(result) -> list[bytes]:
    return [x.data for x in result.lines]


def test_attach_to_100_lines_emits_nothing_then_follows(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"".join(b"old %d\n" % i for i in range(100)))
    r = poll_file(str(p), TailState())
    assert r.lines == () and r.state.offset == p.stat().st_size
    append(p, b"new\n")
    assert datas(poll_file(str(p), r.state)) == [b"new"]


def test_file_ending_in_partial_line_attaches_at_its_start(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"done\nhalf wri")
    r = poll_file(str(p), TailState())
    assert r.state.offset == 5 and r.lines == ()
    append(p, b"tten\n")
    assert datas(poll_file(str(p), r.state)) == [b"half written"]


def test_small_file_without_newline_attaches_at_zero(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"only partial")
    r = poll_file(str(p), TailState())
    assert r.state.offset == 0 and not r.state.discarding
    append(p, b"\n")
    assert datas(poll_file(str(p), r.state)) == [b"only partial"]


def test_big_file_without_newline_in_last_window_attaches_at_end_discarding(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"first\n" + b"q" * 20000)
    r = poll_file(str(p), TailState())
    assert r.state.offset == p.stat().st_size and r.state.discarding
    append(p, b"rest of it\nnext\n")
    assert datas(poll_file(str(p), r.state)) == [b"next"]


def test_backfill_returns_last_lines_oldest_first_then_follows(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"".join(b"line %d\n" % i for i in range(20)))
    r = poll_file(str(p), TailState(), backfill_lines=5)
    assert datas(r) == [b"line %d" % i for i in range(15, 20)]
    assert r.lines[-1].end == p.stat().st_size
    append(p, b"line 20\n")
    assert datas(poll_file(str(p), r.state)) == [b"line 20"]


def test_backfill_larger_than_file_returns_all_lines(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"a\nb\nc\n")
    assert datas(poll_file(str(p), TailState(), backfill_lines=1000)) == [b"a", b"b", b"c"]


def test_backfill_is_limited_to_the_last_max_read_bytes(tmp_path):
    p = tmp_path / "a.log"
    lines = [b"line %04d " % i + b"w" * 40 for i in range(200)]
    p.write_bytes(b"\n".join(lines) + b"\n")
    r = poll_file(str(p), TailState(), backfill_lines=1000, max_read=1024)
    got = datas(r)
    assert got and len(got) < 200 and got == lines[-len(got):]
    assert got[0] in lines and len(b"\n".join(got)) <= 1024
    assert all(len(x) == 50 for x in got)  # the cut partial first line is not returned


def test_backfill_zero_returns_nothing(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"a\nb\n")
    assert poll_file(str(p), TailState(), backfill_lines=0).lines == ()


def test_persisted_state_resumes_changes_start_by_identity(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"one\ntwo\nthree\n")
    s = poll_file(str(p), TailState()).state
    restored = TailState(ident=s.ident, offset=s.offset, discarding=False)
    append(p, b"four\n")
    r = poll_file(str(p), restored)
    assert r.reason == "resumed" and datas(r) == [b"four"]

    # a stored offset beyond the size: start at 0
    beyond = TailState(ident=s.ident, offset=s.offset + 1000)
    r2 = poll_file(str(p), beyond)
    assert r2.reason == "truncated" and datas(r2) == [b"one", b"two", b"three", b"four"]

    # a different identity: start at 0
    other = TailState(ident=FileIdent(s.ident.dev, s.ident.ino + 12345, s.ident.head_len, s.ident.head_sha),
                      offset=s.offset)
    r3 = poll_file(str(p), other)
    assert r3.reason == "rotated" and datas(r3)[0] == b"one"
