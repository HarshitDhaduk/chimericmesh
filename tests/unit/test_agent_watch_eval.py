"""evaluate_work (task 5.1): the work set. One test per sub-criterion 4.3a.1 to 4.3a.6 and 4.3b (review L8)."""
from __future__ import annotations
import time
from agent_helpers import make_incident as inc
from agent_helpers import make_proposal as prop
from chimericmesh.agent.watch import WorkResult, evaluate_work
def run(incidents=(), proposals=(), *, service_id=None, exclude=frozenset(), registered=lambda s: s in ("svc", "other"),
        approval_of=lambda p: {"by": "user", "rule_id": None, "rule_status_now": None, "approved_at": 1.0,
                               "expired": False}, limit=5) -> WorkResult:
    return evaluate_work(list(incidents), list(proposals), service_id=service_id, exclude=exclude,
                         registered=registered, approval_of=approval_of, limit=limit)
def ids(result: WorkResult) -> list[str]:
    return [i["id"] for i in result.incidents]
def test_4_3a_1_resolved_incident_is_excluded():
    assert ids(run([inc("i1"), inc("i2", resolved=True)])) == ["i1"]
def test_4_3a_2_incident_of_a_deleted_service_is_excluded():
    assert ids(run([inc("i1", service_id="gone"), inc("i2", service_id="svc")])) == ["i2"]
def test_4_3a_2_unassigned_incident_only_when_unassigned_is_requested():
    both = [inc("i1", service_id=None), inc("i2", service_id="svc")]
    assert ids(run(both)) == ["i2"]
    assert ids(run(both, service_id="unassigned")) == ["i1"]
    assert ids(run([inc("i3", service_id="gone")], service_id="unassigned")) == []
def test_4_3a_3_pending_and_approved_proposals_exclude_the_incident():
    for status, hidden in (("pending", True), ("approved", True), ("done", False), ("denied", False)):
        out = run([inc("i1")], [prop("p1", "i1", status=status)])
        assert (ids(out) == []) is hidden, status
def test_4_3a_3_a_proposal_of_another_incident_does_not_hide():
    assert ids(run([inc("i1")], [prop("p1", "i2", status="pending")])) == ["i1"]
def test_4_3a_4_exclude_contains_the_incident_id():
    assert ids(run([inc("i1"), inc("i2")], exclude=frozenset({"i1"}))) == ["i2"]
def test_4_3a_5_user_denial_of_the_current_opening_hides_the_incident():
    p = prop("p1", "i1", status="denied", decision_origin="user", incident_opening=2)
    assert ids(run([inc("i1", opening=2)], [p])) == []
def test_4_3a_5_hub_denial_does_not_hide_the_incident():
    p = prop("p1", "i1", status="denied", decision_origin="hub", incident_opening=2)
    assert ids(run([inc("i1", opening=2)], [p])) == ["i1"]
def test_4_3a_5_user_denial_of_an_earlier_opening_does_not_hide_a_reopened_incident():
    p = prop("p1", "i1", status="denied", decision_origin="user", incident_opening=1)
    assert ids(run([inc("i1", opening=2)], [p])) == ["i1"]
def test_4_3a_5_only_decision_origin_is_read_not_the_decider_name():
    spoof = prop("p1", "i1", status="denied", decision_origin="user", incident_opening=1, decided_by="expired-bob")
    assert ids(run([inc("i1", opening=1)], [spoof])) == []
    hub = prop("p2", "i2", status="denied", decision_origin="hub", incident_opening=1, decided_by="alice")
    assert ids(run([inc("i2", opening=1)], [hub])) == ["i2"]
def test_4_3a_6_service_filter():
    pool = [inc("i1", service_id="svc"), inc("i2", service_id="other")]
    assert ids(run(pool, service_id="svc")) == ["i1"]
    assert ids(run(pool, service_id="other")) == ["i2"]
    assert sorted(ids(run(pool))) == ["i1", "i2"]
    assert ids(run(pool, service_id="gone")) == []
def test_4_3b_approved_proposals_with_the_approval_object():
    seen: list[str] = []
    def approval_of(p):
        seen.append(p.id)
        return {"by": "rule", "rule_id": "r1", "rule_status_now": "active", "approved_at": 5.0, "expired": False}
    out = run([], [prop("p1", "i1", status="approved", files=["/a/b.py"], estimated_lines=3, verification="pytest"),
                   prop("p2", "i2", status="pending"), prop("p3", "i3", status="done"),
                   prop("p4", "i4", status="denied")], approval_of=approval_of)
    assert [p["id"] for p in out.approved_proposals] == ["p1"]
    one = out.approved_proposals[0]
    assert set(one) == {"id", "incident_id", "service_id", "action", "files", "estimated_lines", "verification",
                        "approval"}
    assert one["approval"]["rule_id"] == "r1" and one["files"] == ["/a/b.py"] and one["estimated_lines"] == 3
    assert seen == ["p1"]            # pending, done and denied are never looked up
def test_4_3b_exclude_service_and_unassigned_rules():
    ps = [prop("p1", status="approved", service_id="svc"), prop("p2", status="approved", service_id=None),
          prop("p3", status="approved", service_id="gone"), prop("p4", status="approved", service_id="other")]
    assert [p["id"] for p in run([], ps).approved_proposals] == ["p1", "p4"]
    assert [p["id"] for p in run([], ps, exclude=frozenset({"p1"})).approved_proposals] == ["p4"]
    assert [p["id"] for p in run([], ps, service_id="unassigned").approved_proposals] == ["p2"]
    assert [p["id"] for p in run([], ps, service_id="other").approved_proposals] == ["p4"]
def test_4_3b_at_most_five_in_creation_order():
    ps = [prop(f"p{n}", f"i{n}", status="approved", created=100.0 + n) for n in range(12, 0, -1)]
    out = run([], ps)
    assert [p["id"] for p in out.approved_proposals] == ["p1", "p2", "p3", "p4", "p5"]
def test_incident_order_is_severity_then_count_desc_then_id_and_at_most_five():
    pool = [inc("i9", severity=3, count=5), inc("i1", severity=3, count=5), inc("i2", severity=1, count=1),
            inc("i3", severity=3, count=9), inc("i4", severity=6), inc("i5", severity=5), inc("i6", severity=7)]
    out = ids(run(pool))
    assert out == ["i2", "i3", "i1", "i9", "i5"]
    assert len(out) == 5
def test_incident_fields_and_text_are_capped_and_clean():
    dirty = "x\x00y\r\nz" + "A" * 900
    out = run([inc("i1", sample=dirty, category=dirty, host=dirty)]).incidents[0]
    assert set(out) == {"id", "service_id", "host", "category", "severity", "count", "opening", "first_seen",
                        "last_seen", "sample"}
    assert len(out["sample"]) <= 200 and len(out["host"]) <= 500 and len(out["category"]) <= 500
    for key in ("sample", "host", "category"):
        assert not any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in out[key])
def test_proposal_text_is_capped_and_clean_and_no_event_bodies():
    dirty = "a\x00b\nc" + "B" * 900
    p = prop("p1", status="approved", action=dirty, files=[dirty, "ok"], verification=dirty)
    out = run([], [p], approval_of=lambda _: {"by": dirty, "rule_id": dirty, "rule_status_now": None,
                                              "approved_at": 1.0, "expired": False}).approved_proposals[0]
    assert len(out["action"]) <= 500 and "\x00" not in out["action"] and "\n" not in out["action"]
    assert all(len(f) <= 500 and "\n" not in f for f in out["files"])
    assert len(out["verification"]) <= 500
    assert len(out["approval"]["by"]) <= 500 and "\x00" not in out["approval"]["by"]
def test_approval_of_failure_is_total():
    def boom(_p):
        raise RuntimeError("x")
    out = run([], [prop("p1", status="approved")], approval_of=boom)
    assert out.approved_proposals[0]["approval"] is None
def test_work_result_flag():
    assert not run().incidents and not run().approved_proposals
def test_scale_10000_proposals_and_5000_incidents_under_50_ms():
    incidents = [inc(f"i{n}", severity=n % 8, count=n % 50) for n in range(5000)]
    proposals = [prop(f"p{n}", f"i{n}", status=("approved" if n % 997 == 0 else "done" if n % 2 else "denied"),
                      decision_origin="user", incident_opening=1) for n in range(10_000)]
    start = time.perf_counter()
    out = run(incidents, proposals)
    elapsed = time.perf_counter() - start
    assert len(out.incidents) == 5
    assert elapsed < 0.05, elapsed
