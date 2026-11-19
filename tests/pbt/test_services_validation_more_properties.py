"""Properties 3 and 6 of the services design (protocols subset, too-broad folders)."""
from __future__ import annotations

import os
import re
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.paths import is_too_broad
from chimericmesh.services.validate import KNOWN_PROTOCOLS, validate_service_fields

# ---- Property 3: protocols are a subset ------------------------------------------------------

proto_entry = st.one_of(st.sampled_from(KNOWN_PROTOCOLS), st.text(max_size=12), st.sampled_from(["IRC", " irc", "http", "irc\n"]))


@settings(max_examples=300, deadline=None)
@given(st.lists(proto_entry, max_size=8))
def test_property_3_protocols_are_a_subset(entries):
    raw = {"id": "svc", "name": "n", "protocols": entries}
    if all(e in KNOWN_PROTOCOLS for e in entries):
        got = validate_service_fields(raw, for_create=True)
        assert set(got.protocols) == set(entries)
        assert len(got.protocols) == len(set(got.protocols))
        assert list(got.protocols) == list(dict.fromkeys(entries))
    else:
        with pytest.raises(ValidationFailed) as exc:
            validate_service_fields(raw, for_create=True)
        assert exc.value.field == "protocols"


# ---- Property 6: too broad -------------------------------------------------------------------

@pytest.fixture(scope="module")
def base():
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.realpath(tmp)


def _parts(p: str) -> list[str]:
    return [c for c in re.split(r"[\\/]", os.path.splitdrive(os.path.normcase(os.path.realpath(p)))[1]) if c]


def _is_prefix(a: list[str], b: list[str]) -> bool:
    """``a`` is ``b`` or an ancestor of ``b``."""
    return len(a) <= len(b) and b[: len(a)] == a


names = st.sampled_from(["a", "b", "c"])
rel = st.lists(names, min_size=0, max_size=4)


def _join(base: str, comps: list[str]) -> str:
    return os.path.join(base, *comps) if comps else base


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(folder=rel, home=rel, protected=st.lists(rel, max_size=3))
def test_property_6_matches_the_definition(base, folder, home, protected):
    f, h = _join(base, ["w"] + folder), _join(base, ["w"] + home)
    prot = [_join(base, ["w"] + p) for p in protected]
    fp, hp = _parts(f), _parts(h)
    expected = _is_prefix(fp, hp)  # equals or contains home
    for p in prot:
        pp = _parts(p)
        expected = expected or _is_prefix(fp, pp) or _is_prefix(pp, fp)
    got = is_too_broad(f, protected=prot, home=h)
    assert (got is not None) == expected
    if got is not None:
        assert isinstance(got, str) and got


@settings(max_examples=50, deadline=None)
@given(st.lists(st.sampled_from(["a", "b"]), max_size=3))
def test_property_6_roots_are_always_too_broad(extra):
    root = os.path.abspath(os.sep)
    assert is_too_broad(root, protected=[], home=os.path.join(root, "nowhere", *extra)) is not None
