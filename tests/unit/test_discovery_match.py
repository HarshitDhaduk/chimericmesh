"""Matching and suggestions (R4). Sub-criteria 4.3a to 4.3d."""
from __future__ import annotations

import itertools
import json
import threading

import pytest

from chimericmesh.discovery.catalog import fingerprint, load_catalog
from chimericmesh.discovery.match import build_suggestions
from chimericmesh.discovery.models import Listener, ProcEntry, Snapshot
from chimericmesh.discovery.text import path_key

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})


def rule(path, fmt="plain", kind="other"):
    return {"path": path, "format": fmt, "kind": kind}


def app(app_id, procs, ports, linux=(), windows=(), macos=(), label="x"):
    return {"app_id": app_id, "name": app_id.title(), "process_names": procs, "ports": ports,
            "protocol_label": label, "justification": "test entry",
            "log_paths": {"linux": list(linux), "windows": list(windows), "macos": list(macos)}}


def catalog_of(*apps):
    load = load_catalog(FORMATS, text=json.dumps({"version": 1, "apps": list(apps)}))
    assert load.catalog is not None, load.reason
    return load.catalog


NGINX = app("nginx", ["nginx"], [80, 443], linux=[rule("/var/log/nginx/access*.log", "ncsa", "access"),
                                                     rule("/var/log/nginx/error.log")])
VSFTPD = app("vsftpd", ["vsftpd"], [21], linux=[rule("/var/log/vsftpd.log", "plain", "ftp"),
                                                 rule("/var/log/xferlog", "xferlog", "ftp")])
PROFTPD = app("proftpd", ["proftpd"], [21], linux=[rule("/var/log/proftpd/proftpd.log", "plain", "ftp")])
FILEZ = app("filezilla", ["filezilla server"], [21, 80],
            windows=[rule("%ProgramData%\\fz\\logs\\*.log", "plain", "ftp")])
IIS = app("iis", ["w3wp"], [80], windows=[rule("%SystemDrive%\\inetpub\\logs\\LogFiles\\W3SVC*\\*.log", "w3c", "access")])
SQUID = app("squid", ["squid"], [3128], linux=[rule("/var/log/squid/access.log")])
CAT = catalog_of(NGINX, VSFTPD, PROFTPD, FILEZ, IIS, SQUID)


class FakeFs:
    def __init__(self, files=None, dirs=None, truncated=(), clock=None, step=0.0, unreadable=()):
        self.files = files or {}                      # path -> (size, mtime)
        self.dirs = dirs or {}                        # dir -> [names]
        self.truncated = set(truncated)
        self.clock, self.step = clock, step
        self.unreadable = set(unreadable)
        self.calls = 0

    def _tick(self):
        self.calls += 1
        if self.clock is not None:
            self.clock[0] += self.step

    def stat(self, path):
        self._tick()
        if path in self.files:
            size, mtime = self.files[path]
            return True, size, mtime
        return False, None, None

    def readable(self, path):
        return path in self.files and path not in self.unreadable

    def listdir(self, directory, limit):
        self._tick()
        names = list(self.dirs.get(directory, []))
        return names[:limit], len(names) > limit or directory in self.truncated


def snap(procs=(), listeners=()):
    return Snapshot(tuple(ProcEntry(p, n) for p, n in procs), tuple(listeners), False, (), True)


def build(snapshot, os_name="linux", *, catalog=CAT, fs=None, covered=None, dismissed=frozenset(), env=None,
          deadline=1e9, cancel=None, clock=lambda: 0.0, limit=50, counter=None):
    counter = counter or itertools.count(1)
    return build_suggestions(catalog, snapshot, os_name, env=env or {}, fs=fs or FakeFs(), covered=covered or {},
                             dismissed=dismissed, next_id=lambda: f"sg{next(counter)}", deadline=deadline,
                             cancel=cancel or threading.Event(), clock=clock, limit=limit)


def by_id(result):
    return {s.app_id: s for s in result.suggestions}


def test_4_3_high_confidence_has_every_field_and_no_command_line():
    fs = FakeFs(files={"/var/log/nginx/access.log": (120, 5.0), "/var/log/nginx/error.log": (0, 1.0)},
                dirs={"/var/log/nginx": ["access.log", "error.log"]})
    res = build(snap([(812, "nginx"), (813, "nginx")], [Listener(80, "tcp", 812, "nginx")]), fs=fs)
    sug = by_id(res)["nginx"]
    d = sug.to_dict(state="new", service_id=None)
    assert set(d) == {"suggestion_id", "app_id", "name", "protocol_label", "confidence", "evidence",
                      "candidate_log_paths", "state", "service_id"}
    assert d["confidence"] == "high" and d["suggestion_id"] == "sg1"
    assert d["evidence"] == {"process": "nginx", "pid": 812, "process_count": 2, "ports": [80]}
    assert [c["path"] for c in d["candidate_log_paths"]] == ["/var/log/nginx/access.log", "/var/log/nginx/error.log"]
    assert set(d["candidate_log_paths"][0]) == {"path", "format", "kind", "exists", "readable", "size_bytes", "watched_by"}
    assert d["candidate_log_paths"][0]["size_bytes"] == 120 and d["candidate_log_paths"][0]["format"] == "ncsa"
    assert "code_folder" not in json.dumps(d) and "cmdline" not in json.dumps(d)


def test_4_3_low_confidence_has_no_process():
    res = build(snap(listeners=[Listener(21, "tcp", None, None)]))
    sug = by_id(res)
    assert sug["vsftpd"].confidence == "low" and sug["proftpd"].confidence == "low"
    ev = sug["vsftpd"].evidence
    assert (ev.process, ev.pid, ev.process_count, ev.ports) == (None, None, 0, (21,))


@pytest.mark.parametrize("name,os_name", [("NGINX.EXE", "windows"), ("/usr/sbin/nginx", "linux"), ("nginx", "macos")])
def test_4_1_process_names_are_matched_after_normalization(name, os_name):
    from chimericmesh.discovery.text import normalize_process_name
    cat = catalog_of(app("nginx", ["nginx"], [80], linux=[rule("/l/a.log")], windows=[rule("C:\\l\\a.log")],
                         macos=[rule("/l/a.log")]))
    res = build(snap([(1, normalize_process_name(name, os_name))]), os_name, catalog=cat)
    assert [s.app_id for s in res.suggestions] == ["nginx"]


def test_4_1_linux_comm_truncated_to_15_characters_matches():
    cat = catalog_of(app("fz", ["filezilla-server"], [21], linux=[rule("/l/a.log")]))
    res = build(snap([(1, "filezilla-serve")]), catalog=cat)
    assert [s.app_id for s in res.suggestions] == ["fz"]
    assert build(snap([(1, "other")]), catalog=cat).suggestions == ()


def test_4_2_port_80_alone_gives_nothing_and_port_21_gives_low():
    assert build(snap(listeners=[Listener(80, "tcp", None, None), Listener(443, "tcp", None, None)])).suggestions == ()
    assert set(by_id(build(snap(listeners=[Listener(21, "tcp", None, None)])))) == {"vsftpd", "proftpd"}


def test_4_2_claimed_port_rule_hides_other_ftp_entries():
    res = build(snap([(900, "vsftpd")], [Listener(21, "tcp", 900, "vsftpd")]))
    assert set(by_id(res)) == {"vsftpd"} and by_id(res)["vsftpd"].confidence == "high"


def test_4_2_port_only_needs_a_port_specific_to_the_entry():
    # squid listens on 3128 (port-only), nginx on 80 (never)
    res = build(snap(listeners=[Listener(3128, "tcp", None, None), Listener(80, "tcp", None, None)]))
    assert set(by_id(res)) == {"squid"}


def test_4_5_three_processes_merge_into_one_suggestion_with_lowest_pid():
    res = build(snap([(30, "nginx"), (10, "nginx"), (20, "nginx"), (10, "nginx")],
                     [Listener(80, "tcp", 20, "nginx"), Listener(443, "tcp", 30, "nginx")]))
    assert len(res.suggestions) == 1
    ev = res.suggestions[0].evidence
    assert ev.pid == 10 and ev.process_count == 3 and ev.ports == (80, 443)


def test_4_4_existing_first_missing_kept_and_wildcard_without_match_gives_none():
    fs = FakeFs(files={"/var/log/nginx/error.log": (1, 1.0)}, dirs={"/var/log/nginx": ["error.log"]})
    res = build(snap([(1, "nginx")]), fs=fs)
    cands = res.suggestions[0].candidates
    assert [(c.path, c.exists) for c in cands] == [("/var/log/nginx/error.log", True)]
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/missing.log"), rule("/l/x.log"), rule("/l/dir*/y.log")]))
    fs = FakeFs(files={"/l/x.log": (3, 1.0)})
    res = build(snap([(1, "a")]), catalog=cat, fs=fs)
    assert [(c.path, c.exists) for c in res.suggestions[0].candidates] == [("/l/x.log", True), ("/l/missing.log", False)]


def test_4_4_two_rules_with_the_same_path_keep_the_first():
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/x.log", "ncsa", "access"), rule("/l/x.log", "plain", "error")]))
    res = build(snap([(1, "a")]), catalog=cat)
    assert [(c.format, c.kind) for c in res.suggestions[0].candidates] == [("ncsa", "access")]


def test_4_4_more_than_25_candidates_are_cut_and_the_scan_is_limited():
    paths = [f"/l/f{i:02d}.log" for i in range(40)]
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/*.log")]))
    fs = FakeFs(files={p: (1, 1.0) for p in paths}, dirs={"/l": [p[3:] for p in paths]})
    res = build(snap([(1, "a")]), catalog=cat, fs=fs)
    assert len(res.suggestions[0].candidates) == 25
    assert res.limited and res.reason == "candidate list truncated"


def test_wildcard_staleness_newest_by_mtime_is_first_and_survives_the_cap():
    names = [f"u_ex{i:04d}.log" for i in range(120)]
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/*.log")]))
    # the newest file sorts first by name here: reverse so the newest sorts last
    files = {f"/l/{n}": (1, float(1000 - i)) for i, n in enumerate(names)}
    files["/l/u_ex0119.log"] = (1, 99999.0)
    fs = FakeFs(files=files, dirs={"/l": names})
    res = build(snap([(1, "a")]), catalog=cat, fs=fs)
    paths = [c.path for c in res.suggestions[0].candidates]
    assert paths[0] == "/l/u_ex0119.log" and len(paths) <= 25
    assert res.limited and res.reason == "candidate list truncated"


def test_a_truncated_directory_listing_sets_limited_with_a_reason():
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/*.log")]))
    fs = FakeFs(files={"/l/a.log": (1, 1.0)}, dirs={"/l": ["a.log"]}, truncated={"/l"})
    res = build(snap([(1, "a")]), catalog=cat, fs=fs)
    assert res.limited and res.reason == "candidate list truncated"


def covered_catalog():
    return catalog_of(app("a", ["a"], [], linux=[rule("/l/x.log"), rule("/l/y.log")]))


def test_4_6_all_existing_candidates_covered_drops_the_suggestion():
    fs = FakeFs(files={"/l/x.log": (1, 1.0), "/l/y.log": (1, 1.0)})
    res = build(snap([(1, "a")]), catalog=covered_catalog(), fs=fs, covered={"/l/x.log": "s1", "/l/y.log": "s1"})
    assert res.suggestions == ()


def test_4_6_one_uncovered_existing_candidate_keeps_it_with_watched_by():
    fs = FakeFs(files={"/l/x.log": (1, 1.0), "/l/y.log": (1, 1.0)})
    res = build(snap([(1, "a")]), catalog=covered_catalog(), fs=fs, covered={"/l/x.log": "svc"})
    got = {c.path: c.watched_by for c in res.suggestions[0].candidates}
    assert got == {"/l/x.log": "svc", "/l/y.log": None}


def test_4_6_no_existing_candidate_and_one_covered_missing_drops_it():
    res = build(snap([(1, "a")]), catalog=covered_catalog(), covered={"/l/x.log": "svc"})
    assert res.suggestions == ()
    res = build(snap([(1, "a")]), catalog=covered_catalog())
    assert len(res.suggestions) == 1


def test_4_6_covered_comparison_is_case_insensitive_on_windows():
    cat = catalog_of(app("a", ["a"], [], windows=[rule("C:\\Logs\\X.log")]))
    fs = FakeFs(files={"C:\\Logs\\X.log": (1, 1.0)})
    covered = {path_key("c:/logs/x.LOG", "windows"): "svc"}
    assert build(snap([(1, "a")]), "windows", catalog=cat, fs=fs, covered=covered).suggestions == ()


def test_time_limit_stops_between_candidates_and_marks_limited():
    clock = [0.0]
    cat = catalog_of(app("a", ["a"], [], linux=[rule(f"/l/f{i}.log") for i in range(10)]),
                     app("b", ["b"], [], linux=[rule("/m/x.log")]))
    fs = FakeFs(clock=clock, step=1.0)
    res = build(snap([(1, "a"), (2, "b")]), catalog=cat, fs=fs, deadline=3.0, clock=lambda: clock[0])
    assert res.limited and res.reason == "scan time limit"
    assert fs.calls <= 6
    cancel = threading.Event()
    cancel.set()
    res = build(snap([(1, "a")]), catalog=cat, cancel=cancel)
    assert res.limited and res.reason == "scan time limit" and res.suggestions == ()


def test_two_calls_sharing_one_counter_never_repeat_an_id():
    counter = itertools.count(1)
    a = build(snap([(1, "nginx")]), counter=counter)
    b = build(snap([(1, "nginx"), (2, "vsftpd")]), counter=counter)
    ids = [s.suggestion_id for s in a.suggestions + b.suggestions]
    assert len(ids) == len(set(ids)) == 3


def test_dismissed_pair_is_dropped_and_other_fingerprint_is_not():
    entry = next(e for e in CAT.entries if e.app_id == "nginx")
    fp = fingerprint(entry, "linux")
    assert build(snap([(1, "nginx")]), dismissed=frozenset({("nginx", fp)})).suggestions == ()
    assert len(build(snap([(1, "nginx")]), dismissed=frozenset({("nginx", "0" * 16)})).suggestions) == 1
    assert len(build(snap([(1, "nginx")]), dismissed=frozenset({("other", fp)})).suggestions) == 1


def test_4_8_cap_of_50_high_first_and_ids_only_for_kept():
    apps = [app(f"app{i:02d}", [f"proc{i:02d}"], [], linux=[rule(f"/l/{i}.log")]) for i in range(60)]
    cat = catalog_of(*apps)
    procs = [(i + 1, f"proc{i:02d}") for i in range(60)]
    res = build(snap(procs), catalog=cat)
    assert len(res.suggestions) == 50
    assert [s.suggestion_id for s in res.suggestions] == [f"sg{i}" for i in range(1, 51)]
    # high confidence first: add a port-only entry that comes first in the catalog
    cat2 = catalog_of(app("lowone", ["zzz"], [21], linux=[rule("/l/z.log")]), *apps)
    res = build(snap(procs, [Listener(21, "tcp", None, None)]), catalog=cat2)
    assert res.suggestions[0].confidence == "high" and len(res.suggestions) == 50
    assert "lowone" not in {s.app_id for s in res.suggestions}


def test_hostile_text_never_appears_in_output():
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/*.log")]))
    fs = FakeFs(files={"/l/ok.log": (1, 1.0)},
                dirs={"/l": ["ok.log", "bad\x00.log", "e\x1bsc.log", "nl\n.log"]})
    res = build(snap([(1, "a")]), catalog=cat, fs=fs)
    dumped = json.dumps([s.to_dict(state="new", service_id=None) for s in res.suggestions])
    assert not any(ch in dumped for ch in ("\\u0000", "\\u001b", "\\n"))
    assert [c.path for c in res.suggestions[0].candidates] == ["/l/ok.log"]


def test_readable_and_size_come_from_the_probe():
    cat = catalog_of(app("a", ["a"], [], linux=[rule("/l/x.log")]))
    fs = FakeFs(files={"/l/x.log": (77, 1.0)}, unreadable={"/l/x.log"})
    c = build(snap([(1, "a")]), catalog=cat, fs=fs).suggestions[0].candidates[0]
    assert (c.exists, c.readable, c.size_bytes) == (True, False, 77)
