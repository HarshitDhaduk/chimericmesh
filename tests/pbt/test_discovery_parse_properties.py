"""Property 5: parser totality. Validates: Requirements 2.4, 4.7."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.models import Listener, ProcEntry
from chimericmesh.discovery.parse import (
    parse_lsof,
    parse_netstat_linux,
    parse_netstat_windows,
    parse_ps,
    parse_ss,
    parse_tasklist,
)
from chimericmesh.discovery.text import CONTROL, normalize_process_name

PARSERS = [parse_ss, parse_netstat_linux, parse_tasklist, parse_netstat_windows, parse_ps, parse_lsof]
FRAGMENTS = ["tcp", "udp", "LISTEN", "users:((\"x\",pid=1,fd=2))", "0.0.0.0:80", "[::]:0", "*:*", "\"a,b\"", ",",
             "\x00", "\x1b", "1234/nginx", "TCP", "UDP", "99999", "-1", "\\x20", "\n", " ", "\t", "(LISTEN)",
             "0.0.0.0:65536", "9" * 50]
TEXT = st.one_of(
    st.text(max_size=400),
    st.lists(st.sampled_from(FRAGMENTS), max_size=60).map(" ".join),
    st.binary(max_size=400).map(lambda b: b.decode("utf-8", "replace")),
    st.integers(1, 20).map(lambda n: "tcp LISTEN 0 1 0.0.0.0:80 " * (n * 200)),
)


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: getattr(p, "__name__", "parser"))
@settings(max_examples=60, deadline=None)
@given(TEXT)
def test_parsers_are_total_and_records_are_sane(parser, text):
    out = parser(text)
    assert isinstance(out, tuple) and len(out) <= 20000
    for rec in out:
        assert isinstance(rec, (Listener, ProcEntry))
        assert rec.pid is None or rec.pid >= 0
        name = rec.name
        if name is not None:
            assert CONTROL.search(name) is None and len(name) <= 64
            assert normalize_process_name(name, "linux") == name
        if isinstance(rec, Listener):
            assert 1 <= rec.port <= 65535 and rec.proto in ("tcp", "udp")
