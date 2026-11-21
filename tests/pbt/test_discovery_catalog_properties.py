"""Properties 3 and 4: wildcard expansion and the catalog validator. Validates: Requirements 2.2, 3.1, 3.2, 3.4, 3.5."""
from __future__ import annotations

import copy
import fnmatch
import json
from importlib import resources

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.catalog import validate_catalog
from chimericmesh.discovery.expand import expand_pattern
from chimericmesh.discovery.text import CONTROL
from chimericmesh.parsers.formats import FORMAT_NAMES

KNOWN = frozenset(FORMAT_NAMES)
NAME = st.text(alphabet=list("abc.1/\\\x00\n"), min_size=0, max_size=6)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(["*", "a*", "*.log", "x", "?b", "W*"]), min_size=1, max_size=3),
       st.dictionaries(st.sampled_from(["/r", "/r/a", "/r/b", "/r/x", "/r/a/x"]), st.lists(NAME, max_size=400),
                       max_size=5))
def test_wildcard_expansion_is_bounded_and_sound(components, tree):
    listed = 0
    truncated_seen = False
    calls: list[str] = []

    def lister(directory, limit):
        nonlocal listed, truncated_seen
        calls.append(directory)
        names = tree.get(directory, [])
        out = names[:limit]
        listed += len(out)
        more = len(names) > limit
        truncated_seen |= more
        return out, more

    pattern = "/r/" + "/".join(components)
    res = expand_pattern(pattern, "linux", {}, lister)
    assert listed <= 200
    assert all(c == "/r" or c.startswith("/r/") for c in calls)
    if truncated_seen:
        assert res.truncated
    for path in res.paths:
        parts = path.split("/")[2:]
        assert len(parts) == len(components)
        for part, comp in zip(parts, components):
            assert fnmatch.fnmatchcase(part, comp.replace("[", "[[]")) and "/" not in part
            assert CONTROL.search(part) is None


def _shipped() -> dict:
    return json.loads(resources.files("chimericmesh").joinpath("catalog", "legacy_apps.json").read_text("utf-8"))


JSON_VALUES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)),
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)),
    max_leaves=20)


@settings(max_examples=80, deadline=None)
@given(JSON_VALUES)
def test_validator_is_total_and_stable_under_round_trip(value):
    errs = validate_catalog(value, KNOWN)
    assert isinstance(errs, list)
    if not errs:
        assert validate_catalog(json.loads(json.dumps(value)), KNOWN) == []


def _faults():
    def mut(fn):
        def run(d):
            fn(d)
            return d
        return run
    return [
        mut(lambda d: d.update(version=2)),
        mut(lambda d: d.update(extra=1)),
        mut(lambda d: d["apps"].clear()),
        mut(lambda d: d["apps"][0].update(app_id="BAD")),
        mut(lambda d: d["apps"][1].update(app_id=d["apps"][0]["app_id"])),
        mut(lambda d: d["apps"][0].update(process_names=["X.exe"])),
        mut(lambda d: d["apps"][0].update(ports=[0])),
        mut(lambda d: d["apps"][0]["log_paths"]["linux"][0].update(path="rel/x.log")),
        mut(lambda d: d["apps"][0]["log_paths"]["linux"][0].update(format="nope")),
        mut(lambda d: d["apps"][0]["log_paths"]["linux"][0].update(kind="nope")),
        mut(lambda d: d["apps"][0]["log_paths"]["linux"][0].update(path="/var/log/**/x.log")),
        mut(lambda d: d["apps"][0]["log_paths"].pop("macos")),
        mut(lambda d: d["apps"][0].update(unknown=1)),
        mut(lambda d: d["apps"].append({**copy.deepcopy(d["apps"][0]), "app_id": "mosquitto", "justification": None})),
    ]


FAULTS = _faults()


@settings(max_examples=len(FAULTS) * 2, deadline=None)
@given(st.integers(0, len(FAULTS) - 1))
def test_every_single_fault_is_detected(index):
    data = copy.deepcopy(_shipped())
    assert validate_catalog(data, KNOWN) == []
    assert validate_catalog(FAULTS[index](data), KNOWN)
