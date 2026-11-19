"""Task 4.6: property 14, rotations lose only what was unread."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.connectors.tailread import TailState, poll_file

SET = settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
MAX_READ = 300

OPS = st.lists(
    st.sampled_from(["append", "append", "append", "poll", "poll", "rename", "copytrunc", "delete", "remove"]),
    min_size=200, max_size=200,
)


@SET
@given(OPS, st.lists(st.integers(0, 60), min_size=200, max_size=200))
def test_rotations_lose_only_what_was_unread(ops, pads):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.log"
        p.write_bytes(b"")
        state = poll_file(str(p), TailState(), max_read=MAX_READ).state
        # Lines of the current generation in file order, with the offset after each one.
        gen: list[tuple[bytes, int]] = []
        size = 0
        appended: list[bytes] = []
        lost: set[bytes] = set()
        got: list[bytes] = []
        seq = 0
        rotations = 0
        polled = 0  # the generation the last successful poll saw

        def rotate() -> None:
            nonlocal gen, size
            for body, end in gen:
                # unread at the last poll of this generation (or the generation was never polled)
                if polled != rotations or end > state.offset:
                    lost.add(body)
            gen, size = [], 0

        for i, op in enumerate(ops):
            if op == "append":
                if not p.exists():
                    p.write_bytes(b"")
                body = b"%06d-%d " % (seq, rotations) + b"z" * pads[i]
                seq += 1
                data = body + b"\n"
                with open(p, "ab") as f:
                    f.write(data)
                size += len(data)
                gen.append((body, size))
                appended.append(body)
            elif op == "poll":
                r = poll_file(str(p), state, max_read=MAX_READ)
                assert r.status in ("running", "missing")
                got.extend(x.data for x in r.lines)
                state = r.state
                if r.status == "running":
                    polled = rotations
            elif op == "rename":
                if p.exists():
                    rotate()
                    os.replace(p, str(p) + ".1")
                    p.write_bytes(b"")
                    rotations += 1
            elif op == "copytrunc":
                if p.exists():
                    rotate()
                    with open(p, "r+b") as f:
                        f.truncate(0)
                    rotations += 1
            elif op == "delete":
                if p.exists():
                    rotate()
                    os.remove(p)
                    p.write_bytes(b"")
                    rotations += 1
            elif op == "remove":
                if p.exists():
                    rotate()
                    os.remove(p)
                    rotations += 1
                    r = poll_file(str(p), state, max_read=MAX_READ)  # poll while missing
                    assert r.status == "missing" and r.lines == ()
                    state = r.state
        while p.exists():
            r = poll_file(str(p), state, max_read=MAX_READ)
            got.extend(x.data for x in r.lines)
            state = r.state
            if not r.more:
                break
        assert len(got) == len(set(got)), "a line was emitted twice"
        assert got == [b for b in appended if b not in lost]
