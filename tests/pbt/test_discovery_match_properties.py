"""Properties 6 to 9: match soundness, dedup and caps, control characters, covered rule.
Validates: Requirements 2.7, 4.1, 4.2, 4.5, 4.6, 4.7, 4.8."""
from __future__ import annotations

import itertools
import json
import re
import threading

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.catalog import load_catalog
from chimericmesh.discovery.match import build_suggestions
from chimericmesh.discovery.models import PORT_ONLY_PORTS, Listener, ProcEntry, Scan, Snapshot
from chimericmesh.discovery.text import normalize_process_name, path_key
from chimericmesh.parsers.formats import FORMAT_NAMES

KNOWN = frozenset(FORMAT_NAMES)
CATALOG = load_catalog(KNOWN).catalog
assert CATALOG is not None
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NAMES = sorted({n for e in CATALOG.entries for n in e.process_names})
PORTS = sorted({p for e in CATALOG.entries for p in e.ports} | {8081, 1})


class Fs:
    def __init__(self, files=(), dirs=None):
        self.files, self.dirs = set(files), dirs or {}

    def stat(self, path):
        return (True, 10, 1.0) if path in self.files else (False, None, None)

    def readable(self, path):
        return path in self.files

    def listdir(self, directory, limit):
        names = self.dirs.get(directory, [])
        return names[:limit], len(names) > limit


def run(snapshot, *, fs=None, env=None, covered=None, os_name="linux", dismissed=frozenset()):
    counter = itertools.count(1)
    return build_suggestions(CATALOG, snapshot, os_name, env=env or {}, fs=fs or Fs(), covered=covered or {},
                             dismissed=dismissed, next_id=lambda: f"sg{next(counter)}", deadline=1e18,
                             cancel=threading.Event())


NAME = st.one_of(st.sampled_from(NAMES), st.sampled_from(NAMES).map(str.upper),
                 st.text(max_size=12).map(lambda t: normalize_process_name(t, "linux")))
PROCS = st.lists(st.tuples(st.one_of(st.none(), st.integers(0, 40)), NAME), max_size=40)
LISTENERS = st.lists(st.builds(Listener, st.sampled_from(PORTS), st.sampled_from(["tcp", "udp"]),
                               st.one_of(st.none(), st.integers(0, 40)), st.none()), max_size=30)


def snap(procs, listeners):
    return Snapshot(tuple(ProcEntry(p, normalize_process_name(n, "linux")) for p, n in procs), tuple(listeners),
                    False, (), True)


def matches(entry, name):
    return any(name[:15] == n[:15] for n in entry.process_names)


@settings(max_examples=100, deadline=None)
@given(PROCS, LISTENERS)
def test_property_6_match_soundness(procs, listeners):
    snapshot = snap(procs, listeners)
    res = run(snapshot)
    entries = {e.app_id: e for e in CATALOG.entries}
    matched_pids = {p.pid for p in snapshot.procs if any(matches(e, p.name) for e in CATALOG.entries)}
    claimed = {l.port for l in snapshot.listeners if l.pid is not None and l.pid in matched_pids}
    for s in res.suggestions:
        entry = entries[s.app_id]
        has_proc = any(matches(entry, p.name) for p in snapshot.procs)
        if s.confidence == "high":
            assert has_proc and s.evidence.process is not None
            assert any(matches(entry, p.name) and p.pid == s.evidence.pid for p in snapshot.procs) or s.evidence.pid is None
        else:
            assert not has_proc and s.evidence.process is None and s.evidence.pid is None
            eligible = set(entry.ports) & PORT_ONLY_PORTS
            assert s.evidence.ports and set(s.evidence.ports) <= eligible
            assert not set(s.evidence.ports) & claimed
            assert {l.port for l in snapshot.listeners} >= set(s.evidence.ports)


@settings(max_examples=100, deadline=None)
@given(PROCS, LISTENERS, st.randoms())
def test_property_7_dedup_and_caps(procs, listeners, rnd):
    shuffled = list(procs) * 2
    rnd.shuffle(shuffled)
    snapshot = snap(shuffled, listeners)
    res = run(snapshot)
    ids = [s.suggestion_id for s in res.suggestions]
    apps = [s.app_id for s in res.suggestions]
    assert len(apps) == len(set(apps)) and len(ids) == len(set(ids)) and len(ids) <= 50
    entries = {e.app_id: e for e in CATALOG.entries}
    for s in res.suggestions:
        assert len(s.candidates) <= 25
        if s.confidence == "high":
            distinct = {(p.pid, p.name) for p in snapshot.procs if matches(entries[s.app_id], p.name)}
            assert s.evidence.process_count == len(distinct)


HOSTILE = st.text(alphabet=st.sampled_from(list("ab/\\.\x00\x1b\x85\n\r\ud800 nginx")), max_size=30)


@settings(max_examples=80, deadline=None)
@given(st.lists(HOSTILE, max_size=10), st.lists(HOSTILE, max_size=10), HOSTILE)
def test_property_8_control_characters_never_escape(proc_names, dir_names, home):
    procs = ([(i, normalize_process_name(n, "linux")) for i, n in enumerate(proc_names)]
             + [(900, "nginx"), (901, "vsftpd"), (902, "zzcanaryproc")])
    snapshot = Snapshot(tuple(ProcEntry(p, n) for p, n in procs),
                        (Listener(80, "tcp", 900, "nginx"),), False, (), True)
    fs = Fs(files={"/var/log/nginx/access.log"}, dirs={"/var/log/nginx": dir_names + ["access.log"]})
    res = run(snapshot, fs=fs, env={"HOME": home})
    scan = Scan("sc1", "done", 1.0, 2.0, False, "linux", "", (), res.suggestions)
    seen: list[str] = []

    def walk(value):
        if isinstance(value, str):
            seen.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(k)
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(scan.to_dict())
    assert all(CONTROL.search(s) is None for s in seen)
    assert "zzcanaryproc" not in json.dumps(scan.to_dict())      # R2.7: only matched evidence is kept


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(st.booleans(), st.sampled_from(["", "svc"])), min_size=0, max_size=6))
def test_property_9_covered_rule(spec):
    paths = [f"/l/f{i}.log" for i in range(len(spec))]
    rules = [{"path": p, "format": "plain", "kind": "other"} for p in paths]
    if not rules:
        return
    app = {"app_id": "a", "name": "A", "process_names": ["a"], "ports": [], "protocol_label": "x",
           "justification": "t", "log_paths": {"linux": rules, "windows": [], "macos": []}}
    cat = load_catalog(KNOWN, text=json.dumps({"version": 1, "apps": [app]})).catalog
    exists = {p for p, (e, _) in zip(paths, spec) if e}
    covered = {path_key(p, "linux"): c for p, (_, c) in zip(paths, spec) if c}
    res = build_suggestions(cat, Snapshot((ProcEntry(1, "a"),), (), False, (), True), "linux", env={}, fs=Fs(files=exists),
                            covered=covered, dismissed=frozenset(), next_id=lambda: "sg1", deadline=1e18,
                            cancel=threading.Event())
    any_covered = bool(covered)
    existing_all_covered = all(p in {k for k in covered} for p in exists)
    expect_drop = any_covered and existing_all_covered
    assert (len(res.suggestions) == 0) == expect_drop
    for s in res.suggestions:
        for c in s.candidates:
            assert (c.watched_by is not None) == (path_key(c.path, "linux") in covered)
