"""Property tests for IRC replay helpers."""

import re

from hypothesis import given, strategies as st

from chimericmesh.replay_irc import Masker, parse_ubuntu_line, safe_nick

nicks = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n\x00 <>"),
    min_size=1,
    max_size=20,
)


@given(st.text(max_size=300))
def test_parse_ubuntu_line_never_raises(line):
    result = parse_ubuntu_line(line)
    assert result is None or (result[0] and result[1] and "\n" not in result[1])


@given(st.text(max_size=100))
def test_safe_nick_is_irc_safe(nick):
    out = safe_nick(nick)
    assert 1 <= len(out) <= 30
    assert re.fullmatch(r"[A-Za-z0-9_\-\[\]\\`^{|}]+", out)


@given(st.lists(nicks, min_size=1, max_size=60), st.text(max_size=200))
def test_masker_is_injective_letters_only_and_total(names, text):
    m = Masker(names)
    values = list(m.map.values())
    assert len(set(values)) == len(values) == len(set(names))
    assert all(re.fullmatch(r"[A-Za-z]+", v) for v in values)
    assert isinstance(m.text(text), str)  # total: never raises on arbitrary text


@given(st.lists(nicks, min_size=1, max_size=30, unique=True))
def test_masker_is_deterministic(names):
    assert Masker(names).map == Masker(list(names)).map
