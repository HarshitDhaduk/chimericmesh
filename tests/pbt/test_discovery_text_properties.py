"""Property 1: name normalization. Validates: Requirements 4.1, 4.7."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.text import CONTROL, clean_text, normalize_process_name

OS = st.sampled_from(["windows", "linux", "macos"])
TEXT = st.one_of(
    st.text(max_size=300),
    st.text(alphabet=st.sampled_from(list("ab./\\ .EXEexe\x00\x1b\x85\ud800")), max_size=200),
    st.integers(1, 10_000).map(lambda n: "A" * n + ".EXE"),
)


@settings(max_examples=100, deadline=None)
@given(TEXT, OS)
def test_normalize_process_name_total_bounded_and_idempotent(raw, os_name):
    out = normalize_process_name(raw, os_name)
    assert isinstance(out, str)
    assert len(out) <= 64
    assert CONTROL.search(out) is None
    assert "/" not in out and "\\" not in out
    assert not out.endswith(".exe")
    assert normalize_process_name(out, os_name) == out


@settings(max_examples=60, deadline=None)
@given(st.one_of(TEXT, st.integers(), st.none(), st.binary()), st.integers(0, 3000))
def test_clean_text_never_raises_and_is_clean(value, limit):
    out = clean_text(value, limit)
    assert len(out) <= limit
    assert CONTROL.search(out) is None
