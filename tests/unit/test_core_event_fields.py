import dataclasses

from chimericmesh.events import Event


def test_new_fields_and_defaults():
    e = Event("syslog", "h", "a", 3, "m")
    assert e.id == 0 and e.service_id is None and e.log_source_id is None


def test_events_differing_only_in_id_compare_equal():
    a = Event("syslog", "h", "a", 3, "m", "raw", 5.0)
    assert a == dataclasses.replace(a, id=7)
    assert hash(a) == hash(dataclasses.replace(a, id=7))
    assert a != dataclasses.replace(a, service_id="svc")


def test_replace_works_on_the_frozen_class():
    a = Event("syslog", "h", "a", 3, "m", ts=1.0)
    b = dataclasses.replace(a, id=7, service_id="s1", log_source_id="ls1")
    assert (b.id, b.service_id, b.log_source_id) == (7, "s1", "ls1")
    assert a.id == 0


def test_to_dict_includes_new_keys():
    d = dataclasses.replace(Event("api", "h", "a", 4, "m", ts=1.0), id=9, service_id="s").to_dict()
    assert d["id"] == 9 and d["service_id"] == "s" and d["log_source_id"] is None
    assert d["severity_name"] == "warning"


def test_existing_construction_still_works():
    assert Event("irc", "h", "app", 6, "msg").severity_name == "info"
    assert Event(source="telnet", host="h", app="a", severity=0, message="x", raw="r", ts=2.0).ts == 2.0


def test_positional_construction_keeps_ts_in_seventh_place():
    """(L7) The new fields are appended after ts, so the old positional form is unchanged."""
    e = Event("syslog", "h", "a", 3, "m", "raw", 12.5)
    assert e.ts == 12.5 and e.raw == "raw"
    names = [f.name for f in dataclasses.fields(Event)]
    assert names[-4:] == ["ts", "id", "service_id", "log_source_id"]
