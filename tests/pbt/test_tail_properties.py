"""Task 4.5: properties 13 (exactly-once without rotation), 15 (overlong lines) and 12 (decoding)."""
from __future__ import annotations

import itertools
import re
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.connectors.framing import FrameDecoder
from chimericmesh.connectors.tailread import RawLine, TailState, decode_line, poll_file

SET = settings(max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

OPS = st.lists(
    st.one_of(
        st.tuples(st.just("line"), st.integers(0, 40)),
        st.tuples(st.just("split"), st.integers(0, 40), st.lists(st.integers(0, 100), min_size=1, max_size=2)),
        st.tuples(st.just("crlf"), st.integers(0, 40)),
        st.tuples(st.just("long"), st.integers(0, 0)),
        st.tuples(st.just("poll"), st.integers(0, 0)),
    ),
    max_size=40,
)


def write(path: Path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


@SET
@given(OPS, st.sampled_from([512, 4096, 1 << 20]))
def test_tail_without_rotation_is_exactly_once_and_ordered(ops, max_read):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.log"
        p.write_bytes(b"")
        state = poll_file(str(p), TailState(), max_read=max_read).state
        ids = itertools.count()
        expected: list[bytes] = []
        got: list[bytes] = []

        def poll_all() -> None:
            nonlocal state
            while True:
                r = poll_file(str(p), state, max_read=max_read)
                assert r.status == "running"
                got.extend(x.data for x in r.lines)
                state = r.state
                if not r.more:
                    break

        for op in ops:
            kind = op[0]
            if kind == "poll":
                poll_all()
                again = poll_file(str(p), state, max_read=max_read)
                assert again.lines == ()
                continue
            n = next(ids)
            body = b"id%05d " % n + b"x" * op[1] if kind != "long" else b"id%05d " % n + b"L" * 9000
            if kind == "long":
                expected.append(body[:8192])
                write(p, body + b"\n")
            elif kind == "crlf":
                expected.append(body)
                write(p, body + b"\r\n")
            elif kind == "split":
                data = body + b"\n"
                cuts = sorted({c % len(data) for c in op[2]} | {0, len(data)})
                for a, b in zip(cuts, cuts[1:]):
                    write(p, data[a:b])
                    if a == 0 and b < len(data):
                        poll_all()  # a poll between the pieces must not emit the half line
                        assert got == expected
                expected.append(body)
            else:
                expected.append(body)
                write(p, body + b"\n")
        poll_all()
        assert got == expected


@SET
@given(st.lists(st.integers(1, 5000), min_size=1, max_size=8), st.integers(0, 3))
def test_overlong_lines_are_bounded(chunks, polls_between):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.log"
        p.write_bytes(b"start\n")
        state = poll_file(str(p), TailState()).state
        out = []
        for size in chunks:
            write(p, b"q" * size)
            for _ in range(polls_between):
                r = poll_file(str(p), state, max_read=4096)
                assert r.bytes_read <= 4096 + 256 + 8192
                out.extend(r.lines)
                state = r.state
        write(p, b"\nnext line\n")
        while True:
            r = poll_file(str(p), state, max_read=4096)
            out.extend(r.lines)
            state = r.state
            if not r.more:
                break
        assert all(len(x.data) <= 8192 for x in out)
        total = sum(chunks)
        long_lines = [x for x in out if x.data != b"next line"]
        if total > 8192:
            assert len(long_lines) == 1 and long_lines[0].truncated and long_lines[0].data == b"q" * 8192
        else:
            assert [x.data for x in long_lines] == [b"q" * total]
        assert out[-1].data == b"next line"


@settings(max_examples=2000, deadline=None)
@given(st.binary(max_size=500), st.booleans())
def test_decoding_is_total_and_clean(data, truncated):
    text = decode_line(RawLine(data, 0, truncated))
    assert isinstance(text, str)
    assert not _CONTROL.search(text)
    d = FrameDecoder()
    for frame in d.feed(b"x" + data + b"\n") + d.finish():
        assert isinstance(frame, bytes)
        assert isinstance(frame.decode("utf-8", "replace"), str)
