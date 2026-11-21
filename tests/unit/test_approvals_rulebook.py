from __future__ import annotations

from approvals_rules import make_rule

from chimericmesh.approvals.rulebook import RANK, RuleBook, Snapshot

STATUSES = ("active", "suspended", "revoked", "expired")


def book(*rules, paused=False):
    b = RuleBook()
    b.load(list(rules), paused=paused, paused_by="x" if paused else "", paused_at=1.0 if paused else None)
    return b


def snap(*rules, paused=False):
    return Snapshot(tuple(rules), paused, "snap" if paused else "", 2.0 if paused else None)


def test_load_orders_by_seq_and_paused_defaults_false():
    b = book(make_rule(id="r2", seq=2), make_rule(id="r1", seq=1))
    assert [r.id for r in b.rules()] == ["r1", "r2"]
    assert b.paused is False and b.paused_by == ""
    assert book(paused=True).paused is True


def test_filters():
    b = book(make_rule(id="r1", seq=1), make_rule(id="r2", seq=2, service_id="other", status="revoked"))
    assert [r.id for r in b.rules(service_id="other")] == ["r2"]
    assert [r.id for r in b.rules(status="active")] == ["r1"]
    assert b.get("r9") is None


def test_replace_swaps_the_object_and_bumps_version_and_epoch():
    b = book(make_rule())
    v, e = b.version("r1"), b.epoch
    new = make_rule(status="revoked")
    b.replace(new)
    assert b.get("r1") is new and b.version("r1") == v + 1 and b.epoch == e + 1


def test_dirty_flags():
    b = book(make_rule())
    b.mark_dirty("r1")
    b.mark_paused_dirty()
    assert b.dirty == {"r1"} and b.paused_dirty
    b.clear_dirty("r1")
    b.clear_paused_dirty()
    assert b.dirty == set() and not b.paused_dirty


def test_merge_never_loosens_an_untouched_clean_rule():
    for mirror in STATUSES:
        for snapshot in STATUSES:
            b = book(make_rule(status=mirror))
            b.merge(snap(make_rule(status=snapshot)), started_epoch=b.epoch)
            assert RANK[b.get("r1").status] >= RANK[mirror], (mirror, snapshot)
            assert RANK[b.get("r1").status] >= RANK[snapshot], (mirror, snapshot)


def test_merge_table_for_status():
    cases = [("active", "revoked", "revoked"), ("revoked", "active", "revoked"), ("expired", "active", "expired"),
             ("suspended", "active", "suspended"), ("active", "suspended", "suspended"),
             ("active", "active", "active")]
    for mirror, snapshot, expected in cases:
        b = book(make_rule(status=mirror))
        b.merge(snap(make_rule(status=snapshot)), started_epoch=b.epoch)
        assert b.get("r1").status == expected, (mirror, snapshot)


def test_merge_keeps_a_rule_changed_after_the_read_started():
    b = book(make_rule(status="active"))
    started = b.epoch
    b.replace(make_rule(status="revoked", status_reason="mine"))
    b.merge(snap(make_rule(status="active", note="from db")), started_epoch=started)
    assert b.get("r1").status == "revoked" and b.get("r1").note == ""


def test_merge_keeps_a_dirty_rule_whole():
    b = book(make_rule(status="suspended", status_reason="paused"))
    b.mark_dirty("r1")
    b.merge(snap(make_rule(status="active", note="db")), started_epoch=b.epoch)
    assert b.get("r1").status == "suspended" and b.get("r1").note == ""


def test_merge_adds_new_rows_and_takes_other_fields_from_the_snapshot():
    b = book(make_rule())
    b.merge(snap(make_rule(max_lines=50, use_count=7), make_rule(id="r2", seq=2)), started_epoch=b.epoch)
    assert b.get("r1").max_lines == 50 and b.get("r1").use_count == 7
    assert b.get("r2") is not None


def test_merge_with_equal_snapshot_changes_nothing():
    b = book(make_rule())
    v = b.version("r1")
    assert b.merge(snap(make_rule()), started_epoch=b.epoch) == []
    assert b.version("r1") == v


def test_pause_flag_merge_cases():
    b = book(paused=True)
    b.merge(snap(paused=False), started_epoch=b.epoch)
    assert b.paused is True                                     # snapshot false, mirror true
    b = book(paused=False)
    b.merge(snap(paused=True), started_epoch=b.epoch)
    assert b.paused is True                                     # snapshot true, untouched mirror false
    b = book(paused=False)
    started = b.epoch
    b.set_paused(False, "", None)                               # a pause change after the read started
    b.merge(snap(paused=True), started_epoch=started)
    assert b.paused is False                                    # kept: pause_version changed after started_epoch
    b = book(paused=False)
    b.mark_paused_dirty()
    b.merge(snap(paused=True), started_epoch=b.epoch)
    assert b.paused is False                                    # kept: dirty


def test_set_paused_records_who_and_when():
    b = book()
    b.set_paused(True, "irc:bob", 5.0)
    assert (b.paused, b.paused_by, b.paused_at) == (True, "irc:bob", 5.0)
    assert b.pause_version == 1
