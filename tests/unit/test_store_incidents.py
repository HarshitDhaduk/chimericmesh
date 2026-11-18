import dataclasses
import sqlite3

import pytest

from chimericmesh.diagnostics import Incident, classify
from chimericmesh.store.incidents import (
    IncidentRow,
    get_incident_row,
    load_incidents,
    row_to_incident,
    stored_ids_beyond,
    upsert_incident,
)
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "i.db", registry=core_registry())
    yield s
    s.close()


def make_incident(iid="abc1234567", **kw) -> Incident:
    base = dict(id=iid, host="db01", category="disk", signature="no space left", severity=2, count=5,
                first_seen=10.0, last_seen=20.0, sources=["syslog", "irc"], sample="FATAL: No space left on device",
                resolved=False, service_id="svc1", opening=2, resolved_at=None, opened=True)
    base.update(kw)
    return Incident(**base)


def put(store, row):
    store.transact_sync(lambda tx: upsert_incident(tx, row))


def load(store, limit=100):
    return store.read_sync(lambda tx: load_incidents(tx, limit))


def test_round_trip_every_field(store):
    inc = make_incident(resolved=True, resolved_at=33.5)
    put(store, IncidentRow.from_incident(inc, announced_count=3))
    [row] = load(store)
    assert row == IncidentRow.from_incident(inc, announced_count=3)
    assert (row.opened, row.opening, row.resolved_at, row.announced_count) == (True, 2, 33.5, 3)
    assert row.sources == ("syslog", "irc") and row.service_id == "svc1"


def test_hypothesis_and_remediation_come_back_through_classify(store):
    put(store, IncidentRow.from_incident(make_incident(), None))
    [row] = load(store)
    inc = row_to_incident(row)
    category, hypothesis, steps = classify(row.sample)
    assert (inc.hypothesis, inc.remediation) == (hypothesis, list(steps))
    assert inc.sources == ["syslog", "irc"] and inc.opened is True and inc.count == 5


def test_evicted_rows_stay_and_unloaded_ids_are_listed(store):
    for n in range(5):
        put(store, IncidentRow.from_incident(make_incident(f"id{n}", last_seen=float(n)), None))
    assert [r.id for r in load(store, 2)] == ["id4", "id3"]
    assert sorted(store.read_sync(lambda tx: stored_ids_beyond(tx, 2))) == ["id0", "id1", "id2"]
    assert len(load(store)) == 5


def test_resolved_without_resolved_at_is_rejected(store):
    row = IncidentRow.from_incident(make_incident(resolved=True, resolved_at=1.0), None)
    bad = dataclasses.replace(row, resolved_at=None)
    with pytest.raises(sqlite3.IntegrityError):
        put(store, bad)


def test_row_is_immutable_and_cuts_sample_and_host():
    row = IncidentRow.from_incident(make_incident(sample="x" * 9000, host="h" * 300), None)
    assert len(row.sample) == 4096 and len(row.host) == 255
    assert isinstance(row.sources, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.count = 1  # type: ignore[misc]


def test_upsert_always_writes_the_snapshot_announced_count(store):
    inc = make_incident()
    seen = []
    for marker in (None, 3, 1):
        put(store, IncidentRow.from_incident(inc, marker))
        seen.append(store.read_sync(lambda tx: get_incident_row(tx, inc.id)).announced_count)
    assert seen == [None, 3, 1]
    assert get_incident_row.__name__  # exported


def test_get_incident_row_missing_is_none(store):
    assert store.read_sync(lambda tx: get_incident_row(tx, "nope")) is None


def test_upsert_has_no_case_expression():
    import chimericmesh.store.incidents as mod
    assert "CASE" not in mod._UPSERT.upper()
    assert "ON CONFLICT(id) DO UPDATE SET" in mod._UPSERT
