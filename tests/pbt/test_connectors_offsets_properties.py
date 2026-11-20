"""Task 6.3: property 17, offsets round trip and resume (poll level; the tailer level is covered by task 7)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.connectors.offsets import OffsetStore, resume_state, row_from_state
from chimericmesh.connectors.schema import connectors_registry
from chimericmesh.connectors.tailread import FileIdent, TailState, poll_file
from chimericmesh.store import Store

SET = settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))

STATES = st.builds(
    TailState,
    ident=st.builds(FileIdent, dev=st.integers(0, 2**64 - 1), ino=st.integers(0, 2**64 - 1),
                    head_len=st.integers(0, 256), head_sha=st.text("0123456789abcdef", min_size=64, max_size=64)),
    offset=st.integers(0, 2**62), discarding=st.booleans(),
)


@SET
@given(st.lists(STATES, min_size=1, max_size=5))
def test_row_state_round_trip_through_the_store(states):
    with tempfile.TemporaryDirectory() as d:
        store = Store.open(Path(d) / "o.db", registry=connectors_registry())
        try:
            offsets = OffsetStore(store)
            rows = [row_from_state(f"s{i}", s, 1.0) for i, s in enumerate(states)]
            loaded = asyncio.run(_flush_load(offsets, rows))
        finally:
            store.close()
        for i, s in enumerate(states):
            back = resume_state(loaded[f"s{i}"])
            assert (back.ident, back.offset, back.discarding) == (s.ident, s.offset, s.discarding)


async def _flush_load(offsets, rows):
    await offsets.flush(rows)
    return await offsets.load()


def _drain(path: str, state: TailState, out: list[bytes], limit: int | None = None) -> TailState:
    """Poll until idle; with ``limit``, stop (gracefully) after that many lines, at the last emitted line."""
    while True:
        r = poll_file(path, state, max_read=512)
        state = r.start
        for line in r.lines:
            if limit is not None and len(out) >= limit:
                return state
            out.append(line.data)
            state = TailState(ident=r.state.ident, offset=line.end, discarding=line.discarding_after,
                              mtime_ns=0)
        state = r.state
        if not r.more:
            return state


@SET
@given(st.lists(st.binary(max_size=40).map(lambda b: b.replace(b"\n", b".")), min_size=1, max_size=60),
       st.integers(0, 60), st.booleans())
def test_graceful_stop_and_resume_emit_each_line_once(lines, stop_after, rotate):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.log"
        p.write_bytes(b"")
        start = poll_file(str(p), TailState()).state
        body = b"".join(b"%04d %s\n" % (i, x) for i, x in enumerate(lines))
        p.write_bytes(body)
        expected = [b"%04d %s" % (i, x) for i, x in enumerate(lines)]

        first: list[bytes] = []
        stopped = _drain(str(p), start, first, limit=stop_after)
        store = Store.open(Path(d) / "o.db", registry=connectors_registry())
        try:
            offsets = OffsetStore(store)
            row = row_from_state("src", stopped, 1.0)
            loaded = asyncio.run(_flush_load(offsets, [row]))
        finally:
            store.close()
        resumed = resume_state(loaded["src"])
        if rotate:
            os.replace(p, str(p) + ".1")
            p.write_bytes(b"new 1\nnew 2\n")
            second: list[bytes] = []
            _drain(str(p), resumed, second)
            assert second == [b"new 1", b"new 2"]  # identity changed: the new file from its start
        else:
            second = []
            _drain(str(p), resumed, second)
            assert first + second == expected
