"""Property 2: placeholder expansion allow-list. Validates: Requirements 3.2, 3.6."""
from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.expand import ALLOWED_ENV, expand_leading_env, expand_pattern
from chimericmesh.discovery.text import CONTROL

OS = st.sampled_from(["windows", "linux", "macos"])
ALPHABET = list("%$*?/\\:.abcHOMEPATHSystemDriveProgramData() \x00\x1b-_[]")
NAMES = st.sampled_from(sorted({n for v in ALLOWED_ENV.values() for n in v}) + ["PATH", "SECRET", "X", "home"])
PATTERNS = st.one_of(
    st.text(alphabet=ALPHABET, max_size=80),
    st.builds(lambda n, rest, w: (f"%{n}%" if w else f"${n}") + rest, NAMES,
              st.text(alphabet=ALPHABET, max_size=40), st.booleans()),
)
VALUES = st.one_of(st.text(alphabet=ALPHABET, max_size=60), st.just("C:"), st.just("/home/u"),
                   st.just("C:\\ProgramData"), st.just("/tmp/*%X%$HOME"), st.integers(), st.none())


class Spy(Mapping):
    def __init__(self, data):
        self.data = data
        self.looked_up: list[str] = []

    def __getitem__(self, key):
        self.looked_up.append(key)
        return self.data[key]

    def get(self, key, default=None):
        self.looked_up.append(key)
        return self.data.get(key, default)

    def __iter__(self):
        raise AssertionError("the environment must not be iterated")

    def __len__(self):
        return len(self.data)


@settings(max_examples=100, deadline=None)
@given(PATTERNS, OS, st.dictionaries(NAMES, VALUES, max_size=6))
def test_only_allow_listed_names_are_read_and_base_is_clean(pattern, os_name, env):
    spy = Spy(env)
    out = expand_leading_env(pattern, os_name, spy)
    allowed = {n.casefold() for n in ALLOWED_ENV[os_name]}
    assert all(k.casefold() in allowed for k in spy.looked_up)
    if out is not None:
        base, rest = out
        assert CONTROL.search(base) is None and base
        assert pattern.endswith(rest)
        if os_name == "windows":
            assert base[1:3] == ":\\" or base.startswith("\\\\") or base[1:2] == ":"
        else:
            assert base.startswith("/")


@settings(max_examples=60, deadline=None)
@given(PATTERNS, OS, st.dictionaries(NAMES, VALUES, max_size=6))
def test_expand_pattern_never_raises_and_never_lists_outside_base(pattern, os_name, env):
    calls: list[str] = []

    def lister(directory, limit):
        calls.append(directory)
        return ["a.log", "b"], False

    res = expand_pattern(pattern, os_name, Spy(env), lister)
    assert all(CONTROL.search(p) is None for p in res.paths)
    led = expand_leading_env(pattern, os_name, Spy(env))
    if led is not None:
        assert all(c.startswith(led[0].rstrip("\\/")) for c in calls)
