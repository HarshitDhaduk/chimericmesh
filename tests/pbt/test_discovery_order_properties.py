"""Property 10: existing paths first, newest first within a wildcard. Validates: Requirements 4.4."""
from __future__ import annotations

import json
import threading

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.catalog import load_catalog
from chimericmesh.discovery.match import build_suggestions
from chimericmesh.discovery.models import ProcEntry, Snapshot

KNOWN = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
LITERALS = ["/l/a.log", "/l/b.log", "/l/c.log", "/l/d.log"]


class Fs:
    def __init__(self, existing, mtimes, names):
        self.existing, self.mtimes, self.names = set(existing), mtimes, names

    def stat(self, path):
        return (True, 1, self.mtimes.get(path, 1.0)) if path in self.existing else (False, None, None)

    def readable(self, path):
        return path in self.existing

    def listdir(self, directory, limit):
        names = self.names.get(directory, [])
        return names[:limit], len(names) > limit


@settings(max_examples=100, deadline=None)
@given(st.lists(st.sampled_from(LITERALS), unique=True, max_size=4),
       st.sets(st.sampled_from(LITERALS)),
       st.lists(st.tuples(st.text(alphabet="abcdef", min_size=1, max_size=4), st.floats(0, 1e6)), unique_by=lambda t: t[0],
                max_size=60))
def test_existing_first_catalog_order_and_newest_first_within_a_wildcard(literal_rules, existing, wild):
    rules = [{"path": p, "format": "plain", "kind": "other"} for p in literal_rules]
    rules.append({"path": "/w/*.log", "format": "plain", "kind": "other"})
    app = {"app_id": "a", "name": "A", "process_names": ["a"], "ports": [], "protocol_label": "x",
           "justification": "t", "log_paths": {"linux": rules, "windows": [], "macos": []}}
    cat = load_catalog(KNOWN, text=json.dumps({"version": 1, "apps": [app]})).catalog
    wild_paths = {f"/w/{n}.log": m for n, m in wild}
    fs = Fs(set(existing) | set(wild_paths), wild_paths, {"/w": [f"{n}.log" for n, _ in wild]})
    res = build_suggestions(cat, Snapshot((ProcEntry(1, "a"),), (), False, (), True), "linux", env={}, fs=fs,
                            covered={}, dismissed=frozenset(), next_id=lambda: "sg1", deadline=1e18,
                            cancel=threading.Event())
    cands = res.suggestions[0].candidates
    flags = [c.exists for c in cands]
    assert flags == sorted(flags, reverse=True)                       # existing first
    literal_seen = [c.path for c in cands if c.path in LITERALS and c.exists]
    assert literal_seen == [p for p in literal_rules if p in existing]  # catalog order across rules
    wild_seen = [c.path for c in cands if c.path.startswith("/w/")]
    expected = sorted(wild_paths, key=lambda p: (wild_paths[p], p), reverse=True)
    assert wild_seen == expected[:len(wild_seen)]                      # newest first, a prefix survives the cap
    if wild_paths:
        assert wild_seen[:1] == expected[:1]                           # the newest file is always kept

