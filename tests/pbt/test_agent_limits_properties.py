"""Property 4 of agent-loop: safe text. ``clean_text`` and ``cap_items`` (R1.7)."""
from __future__ import annotations

import json
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.agent.limits import MAX_FIELD, MAX_RESPONSE_BYTES, cap_items, clean_text

CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f]")

nasty = st.text(
    alphabet=st.one_of(
        st.sampled_from(["\x00", "\r", "\n", "\x1b", "\x7f", "\x85", "\ud800", "\udfff", "\u2028", " "]),
        st.characters(),
    ),
    max_size=1500,
)
anything = st.one_of(nasty, st.none(), st.integers(), st.floats(allow_nan=True), st.binary(max_size=50))
limits = st.integers(min_value=-5, max_value=1200)


@settings(max_examples=100, deadline=None)
@given(anything, limits)
def test_property_4_clean_text_is_short_and_has_no_control_characters(value, limit):
    out = clean_text(value, limit)
    assert isinstance(out, str)
    assert len(out) <= max(limit, 0)
    assert not CONTROL.search(out)
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in out)
    out.encode("utf-8")                       # always encodable


@settings(max_examples=100, deadline=None)
@given(nasty)
def test_property_4_clean_text_default_limit_and_idempotence(text):
    once = clean_text(text)
    assert len(once) <= MAX_FIELD
    assert clean_text(once) == once


item = st.fixed_dictionaries({"id": st.integers(0, 10**9), "message": nasty.map(lambda t: clean_text(t, 700))})


@settings(max_examples=60, deadline=None)
@given(st.lists(item, max_size=120), st.integers(min_value=60, max_value=MAX_RESPONSE_BYTES))
def test_property_4_cap_items_is_valid_bounded_prefix(items, max_bytes):
    envelope = {"service_id": "svc", "next_after_id": 0}
    out = cap_items(envelope, "events", items, max_bytes=max_bytes)
    raw = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    kept = out["events"]
    assert json.loads(raw.decode("utf-8")) == out                 # valid JSON that parses back
    assert kept == items[: len(kept)]                             # a prefix
    dropped = len(kept) < len(items)
    assert bool(out.get("truncated")) is dropped                  # truncated iff items were dropped
    if dropped or kept:
        assert len(raw) <= max_bytes or not kept                  # an empty list always fits
    if dropped:
        more = dict(out, events=items[: len(kept) + 1], truncated=True)
        assert len(json.dumps(more, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > max_bytes


@settings(max_examples=40, deadline=None)
@given(st.lists(item, max_size=200))
def test_property_4_default_cap_is_20_kb(items):
    out = cap_items({"service_id": "s"}, "events", items)
    assert len(json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= 20_480
