import hashlib

from chimericmesh import diagnostics
from chimericmesh.diagnostics import Incident, IncidentTracker, analyze, incident_id, signature
from chimericmesh.events import Event


def ev(msg="upstream timed out connecting to 10.0.0.1:99", sev=4, host="h1", ts=100.0, source="syslog",
       service_id=None):
    return Event(source, host, "app", sev, msg, "", ts, service_id=service_id)


def open_one(tracker, **kw):
    inc = None
    for i in range(3):
        inc = tracker.add(ev(ts=100.0 + i, **kw))
    return inc


def test_incident_new_fields_and_to_dict():
    inc = Incident("i", "h", "net", "sig", 3, 1, 1.0, 1.0)
    assert (inc.service_id, inc.opening, inc.resolved_at, inc.opened) == (None, 1, None, False)
    d = inc.to_dict()
    assert d["service_id"] is None and d["opening"] == 1 and d["resolved_at"] is None
    assert "opened" not in d
    inc.resolved, inc.resolved_at = True, 55.0
    assert inc.to_dict()["resolved_at"] == 55.0


def test_incident_id_algorithm_is_unchanged():
    sig = signature("upstream timed out connecting to 10.0.0.1:99")
    assert incident_id("h1", sig) == hashlib.sha1(f"h1|{sig}".encode()).hexdigest()[:10]
    t = IncidentTracker()
    assert t.add(ev()).id == incident_id("h1", sig)


def test_preview_id_matches_add_and_changes_nothing():
    t = IncidentTracker()
    e = ev()
    pid = t.preview_id(e)
    assert pid == incident_id("h1", signature(e.message))
    assert t.incidents() == [] and t._clusters == {}
    assert t.add(e).id == pid
    count = t._clusters[pid].count
    assert t.preview_id(e) == pid and t._clusters[pid].count == count


def test_preview_id_is_none_where_add_ignores_the_event():
    t = IncidentTracker()
    for e in (ev(sev=5), ev(sev=7), ev(msg="   "), ev(msg="")):
        assert t.preview_id(e) is None
        assert t.add(e) is None


def test_info_event_never_reopens_a_resolved_incident():
    t = IncidentTracker()
    inc = open_one(t)
    inc.resolved, inc.resolved_at = True, 0.0
    info = ev(sev=6)
    assert t.preview_id(info) is None  # the hub stops here, so the resolved incident stays resolved


def test_opened_is_set_by_add_on_the_first_qualifying_event():
    t = IncidentTracker()
    first = t.add(ev())
    assert first.opened is False
    second = t.add(ev(ts=101.0))
    assert second.opened is False
    third = t.add(ev(ts=102.0))
    assert third.opened is True and third.count == 3


def test_severity_two_opens_on_the_first_event():
    t = IncidentTracker()
    assert t.add(ev(sev=2)).opened is True


def test_opened_incident_stays_returned_with_count_one_and_severity_four():
    t = IncidentTracker()
    inc = open_one(t)
    inc.count, inc.severity = 1, 4
    assert t.qualifies(inc) and t.get(inc.id) is inc and t.incidents() == [inc]


def test_unopened_cluster_is_not_returned():
    t = IncidentTracker()
    inc = t.add(ev())
    assert not t.qualifies(inc) and t.get(inc.id) is None and t.incidents() == []


def test_reopen_resets_the_state_as_designed():
    t = IncidentTracker()
    inc = open_one(t, sev=3)
    inc.resolved, inc.resolved_at = True, 500.0
    inc.sources.append("irc")
    sample, category, signature_, service = inc.sample, inc.category, inc.signature, inc.service_id
    out = t.reopen(inc, ev(sev=4, ts=900.0, source="telnet"))
    assert out is inc
    assert (inc.resolved, inc.resolved_at, inc.opening, inc.count, inc.opened) == (False, None, 2, 1, True)
    assert inc.severity == 4  # the event's severity, not the old minimum
    assert (inc.first_seen, inc.last_seen) == (900.0, 900.0) and inc.sources == ["telnet"]
    assert (inc.sample, inc.category, inc.signature, inc.service_id) == (sample, category, signature_, service)
    assert t.get(inc.id) is inc


def test_reopen_twice_counts_openings():
    t = IncidentTracker()
    inc = open_one(t)
    for expected in (2, 3):
        inc.resolved, inc.resolved_at = True, 1.0
        t.reopen(inc, ev(ts=2.0))
        assert inc.opening == expected


def test_install_adds_a_cluster_that_add_then_continues():
    t = IncidentTracker()
    inc = Incident(incident_id("h1", signature(ev().message)), "h1", "network", signature(ev().message),
                   4, 50, 1.0, 2.0, ["syslog"], "sample", opened=True, opening=2)
    t.install(inc)
    assert t.get(inc.id) is inc
    assert t.preview_id(ev()) == inc.id
    assert t.add(ev(ts=3.0)) is inc and inc.count == 51 and inc.opening == 2


def test_install_over_the_limit_evicts_and_reports(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 2)
    evicted = []
    t = IncidentTracker(on_evict=evicted.append)
    a = t.add(ev(msg="alpha failure one", host="a"))
    t.add(ev(msg="beta failure two", host="b"))
    t.install(Incident("zzz", "c", "unknown", "gamma", 4, 1, 1.0, 1.0))
    assert [i.id for i in evicted] == [a.id] and len(t._clusters) == 2


def test_eviction_keeps_the_baseline_rule_and_calls_on_evict(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 3)
    evicted = []
    t = IncidentTracker(on_evict=evicted.append)
    crit = t.add(ev(msg="kernel panic one", sev=2, host="a", ts=1.0))   # qualifies, oldest
    t.add(ev(msg="minor warn two", host="b", ts=2.0))                    # never qualified, stale
    t.add(ev(msg="minor warn three", host="c", ts=3.0))
    t.add(ev(msg="minor warn four", host="d", ts=4.0))                   # triggers eviction
    assert len(evicted) == 1 and evicted[0].host == "b"
    assert t._clusters.get(crit.id) is crit


def test_eviction_without_callback_still_works(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_CLUSTERS", 1)
    t = IncidentTracker()
    t.add(ev(host="a"))
    t.add(ev(host="b"))
    assert len(t._clusters) == 1


def test_analyze_results_are_unchanged():
    events = [ev(ts=float(i)) for i in range(3)] + [ev(msg="kernel panic", sev=2, host="z")] + \
             [ev(msg="one off error", sev=3, host="y")]
    got = analyze(events)
    assert [(i.host, i.count) for i in got] == [("z", 1), ("h1", 3)]
    assert all("opened" not in i.to_dict() for i in got)


# ---- service_id rule (review 11) -------------------------------------------------------------

def test_first_event_sets_service_id():
    t = IncidentTracker()
    assert t.add(ev(service_id="svc1")).service_id == "svc1"


def test_later_non_null_value_fills_a_null_once():
    t = IncidentTracker()
    inc = t.add(ev())
    assert inc.service_id is None
    t.add(ev(service_id="svc1", ts=101.0))
    assert inc.service_id == "svc1"


def test_non_null_value_is_never_replaced():
    t = IncidentTracker()
    inc = t.add(ev(service_id="svc1"))
    t.add(ev(service_id="svc2", ts=101.0))
    t.add(ev(ts=102.0))
    assert inc.service_id == "svc1"


def test_reopen_keeps_service_id():
    t = IncidentTracker()
    inc = open_one(t, service_id="svc1")
    inc.resolved, inc.resolved_at = True, 1.0
    t.reopen(inc, ev(service_id="svc2", ts=9.0))
    assert inc.service_id == "svc1"


# ---- (M6) `add` sets `opened` itself ----------------------------------------------------------

def test_m6_three_severity_four_events_open_on_the_third():
    t = IncidentTracker()
    seen = [t.add(ev(ts=float(i))).opened for i in range(3)]
    assert seen == [False, False, True]

