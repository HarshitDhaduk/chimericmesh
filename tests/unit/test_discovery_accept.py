"""Accept: validation, the sink port and the acceptor (R5.1, R5.2, R5.3, R5.5, R5.7; review findings 2, 3, 10)."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from chimericmesh.discovery import dismissals
from chimericmesh.discovery.accept import AcceptBody, Acceptor, RegistrySink
from chimericmesh.discovery.errors import AlreadyAccepted, SuggestionNotFound
from chimericmesh.discovery.models import CandidatePath, Evidence, Scan, Suggestion
from chimericmesh.discovery.schema import discovery_registry
from chimericmesh.hub import Hub
from chimericmesh.services.errors import Conflict, SubscriberFailed, ValidationFailed
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})


def sugg(sid="sg1", process="nginx", app_id="nginx"):
    cands = (CandidatePath("/var/log/nginx/access.log", "ncsa", "access", False, False, None, None),)
    return Suggestion(sid, app_id, app_id, "http", "high" if process else "low", Evidence(process, 5, 1, (80,)),
                      cands, "f" * 16)


class StubManager:
    def __init__(self, suggestions, os_name="windows" if os.name == "nt" else "linux"):
        self.scan = Scan("sc1", "done", 1.0, 2.0, False, os_name, "", (), tuple(suggestions))
        self.os_name = os_name

    def find(self, sid):
        for s in self.scan.suggestions:
            if s.suggestion_id == sid:
                return self.scan, s
        return None


class Env:
    def __init__(self, tmp_path, suggestions=None, sink=None):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.data = self.home / ".chimericmesh"
        self.data.mkdir()
        self.store = Store.open(":memory:", registry=discovery_registry())
        self.reg = ServiceRegistry.load(self.store, state=Hub(store=self.store), protected=[str(self.data)],
                                        home=str(self.home))
        self.reg.set_known_formats(FORMATS)
        self.mgr = StubManager(suggestions or [sugg()])
        self.sink = sink or RegistrySink(self.reg)
        self.acc = Acceptor(self.mgr, self.sink, self.reg, self.store, FORMATS)

    def logs(self, *names):
        out = []
        for n in names:
            p = self.tmp / n
            p.write_text("x", encoding="utf-8")
            out.append(str(p))
        return out

    def body(self, paths, sid="svc", **kw):
        return AcceptBody(id=sid, name=kw.pop("name", "Svc"), log_paths=tuple((p, "plain") for p in paths), **kw)

    def accept(self, body, sid="sg1", actor="rest:t"):
        return asyncio.run(self.acc.accept(sid, body, actor=actor))

    def snapshot(self):
        q = lambda sql: self.store.read_sync(lambda tx: [tuple(r) for r in tx.all(sql)], allow_blocking=True)  # noqa: E731
        return (q("SELECT COUNT(*) FROM services"), q("SELECT COUNT(*) FROM log_sources"),
                q("SELECT COUNT(*) FROM service_tombstones"),
                q("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'service_%' OR action LIKE 'log_source_%'"))

    def audit(self):
        return asyncio.run(self.store.audit.read(500))

    def close(self):
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_5_1_nothing_exists_before_accept(env):
    assert env.reg.list() == [] and env.reg.log_sources() == []


def test_5_2a_accept_creates_service_and_sources_through_the_registry(env):
    a, b = env.logs("a.log", "b.log")
    svc, sources = env.accept(env.body([a, b]))
    assert svc.id == "svc" and [s.path for s in sources] == [os.path.normpath(a), os.path.normpath(b)]
    assert env.reg.get("svc") is not None and len(env.reg.log_sources()) == 2
    rows = env.audit()
    created = [r for r in rows if r["action"] == "service_created"]
    added = [r for r in rows if r["action"] == "log_source_added"]
    assert len(created) == 1 and created[0]["actor"] == "discovery" and len(added) == 2


def test_5_2b_every_source_has_origin_discovery_and_protocols_is_file(env):
    svc, sources = env.accept(env.body(env.logs("a.log")))
    assert all(s.origin == "discovery" for s in sources) and svc.protocols == ("file",)
    assert all(s.backfill_lines == 0 for s in sources)


def test_5_2c_body_fields_are_honoured_and_app_names_default_to_the_process(env, tmp_path):
    svc, _ = env.accept(env.body(env.logs("a.log")))
    assert svc.name == "Svc" and svc.app_names == ("nginx",)
    other = tmp_path / "two"
    other.mkdir()
    e2 = Env(other)
    try:
        svc2, _ = e2.accept(e2.body(e2.logs("c.log"), app_names=("one", "two")))
        assert svc2.app_names == ("one", "two")
    finally:
        e2.close()


def test_low_confidence_suggestion_has_no_default_app_names(tmp_path):
    e = Env(tmp_path, [sugg(process=None)])
    try:
        svc, _ = e.accept(e.body(e.logs("a.log")))
        assert svc.app_names == ()
    finally:
        e.close()


BAD_CASES = {
    "bad format": lambda e: AcceptBody("svc", "S", ((e.logs("a.log")[0], "nope"),)),
    "relative path": lambda e: AcceptBody("svc", "S", (("rel/x.log", "plain"),)),
    "directory": lambda e: AcceptBody("svc", "S", ((str(e.tmp), "plain"),)),
    "nul": lambda e: AcceptBody("svc", "S", (("/x\x00y.log", "plain"),)),
    "empty path": lambda e: AcceptBody("svc", "S", (("", "plain"),)),
    "no paths": lambda e: AcceptBody("svc", "S", ()),
    "21 paths": lambda e: AcceptBody("svc", "S", tuple((str(e.tmp / f"f{i}.log"), "plain") for i in range(21))),
    "bad slug": lambda e: AcceptBody("Bad Id", "S", ((e.logs("a.log")[0], "plain"),)),
    "reserved": lambda e: AcceptBody("unassigned", "S", ((e.logs("a.log")[0], "plain"),)),
    "empty name": lambda e: AcceptBody("svc", "", ((e.logs("a.log")[0], "plain"),)),
    "duplicate after normalization": lambda e: AcceptBody(
        "svc", "S", ((e.logs("a.log")[0], "plain"), (str(e.tmp / "x" / ".." / "a.log"), "plain"))),
}


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_5_5_invalid_requests_create_nothing(env, name):
    before = env.snapshot()
    with pytest.raises(ValidationFailed) as info:
        env.accept(BAD_CASES[name](env))
    assert info.value.field
    assert env.snapshot() == before
    assert env.reg.list() == []
    # the marker is cleared: a corrected request for the same suggestion works
    env.accept(env.body(env.logs("ok.log")))


def test_the_invalid_entry_can_be_at_any_position(env):
    good = env.logs("a.log", "b.log", "c.log")
    before = env.snapshot()
    for pos in range(4):
        paths = list(good)
        paths.insert(pos, "relative.log")
        with pytest.raises(ValidationFailed):
            env.accept(env.body(paths))
        assert env.snapshot() == before


def test_code_folder_is_user_typed_passed_through_and_normalized(env):
    folder = env.tmp / "code"
    folder.mkdir()
    svc, _ = env.accept(env.body(env.logs("a.log"), code_folder=str(folder)))
    assert svc.code_folder == os.path.realpath(str(folder))


def root_dir():
    return os.path.abspath(os.sep)


@pytest.mark.parametrize("which", ["home", "root", "data", "relative", "file", "dotdot"])
def test_a_bad_code_folder_is_refused_with_the_services_error_and_nothing_is_created(env, which):
    f = env.logs("note.txt")[0]
    folder = {"home": str(env.home), "root": root_dir(), "data": str(env.data), "relative": "src/app",
              "file": f, "dotdot": str(env.tmp / ".." / "x")}[which]
    before = env.snapshot()
    with pytest.raises(ValidationFailed) as info:
        env.accept(env.body(env.logs("a.log"), code_folder=folder))
    assert info.value.field == "code_folder"
    assert env.snapshot() == before and env.reg.list() == []


def test_suggestion_payloads_never_carry_a_code_folder(env):
    assert "code_folder" not in json.dumps(env.mgr.scan.to_dict())
    env.accept(env.body(env.logs("a.log")))
    assert "code_folder" not in json.dumps(env.mgr.scan.to_dict())


def test_caps_are_pre_checked_with_no_tombstone_and_no_sink_call(tmp_path):
    calls = []

    class SpySink:
        async def create_with_sources(self, fields, sources, *, actor):
            calls.append(fields)
            raise AssertionError("must not be called")

    e = Env(tmp_path, sink=SpySink())
    try:
        async def fill():
            for i in range(10):
                await e.reg.create_service(ServiceFields(id=f"s{i}", name=f"S{i}"), actor="t")
                for j in range(19):
                    await e.reg.add_log_source(f"s{i}", LogSourceFields(path=str(tmp_path / f"l{i}_{j}.log"),
                                                                        format="plain"), actor="t")
        asyncio.run(fill())
        assert len(e.reg.log_sources()) == 190
        before = e.snapshot()
        paths = [str(tmp_path / f"n{i}.log") for i in range(20)]
        with pytest.raises(ValidationFailed) as info:
            e.accept(e.body(paths, sid="new"))
        assert info.value.field == "log_paths"
        assert e.snapshot() == before and calls == []
    finally:
        e.close()


def test_the_services_cap_is_pre_checked(tmp_path):
    e = Env(tmp_path)
    try:
        async def fill():
            for i in range(100):
                await e.reg.create_service(ServiceFields(id=f"s{i}", name=f"S{i}"), actor="t")
        asyncio.run(fill())
        before = e.snapshot()
        with pytest.raises(ValidationFailed) as info:
            e.accept(e.body(e.logs("a.log"), sid="one-more"))
        assert info.value.field == "id" and e.snapshot() == before
    finally:
        e.close()


def test_two_concurrent_accepts_of_one_suggestion_give_one_service(env):
    a = env.logs("a.log")
    b = env.logs("b.log")

    async def go():
        return await asyncio.gather(env.acc.accept("sg1", env.body(a, sid="one"), actor="x"),
                                    env.acc.accept("sg1", env.body(b, sid="two"), actor="x"),
                                    return_exceptions=True)
    res = asyncio.run(go())
    assert sum(isinstance(r, AlreadyAccepted) for r in res) == 1 and sum(isinstance(r, tuple) for r in res) == 1
    assert len(env.reg.list()) == 1


def test_a_second_accept_of_the_same_suggestion_is_refused(env):
    env.accept(env.body(env.logs("a.log")))
    with pytest.raises(AlreadyAccepted):
        env.accept(env.body(env.logs("b.log"), sid="two"))
    assert [s.id for s in env.reg.list()] == ["svc"]


def test_accept_after_a_dismissal_in_the_same_scan_is_allowed(env):
    asyncio.run(dismissals.dismiss(env.store, "nginx", "f" * 16, actor="x", paths=1))
    env.accept(env.body(env.logs("a.log")))
    assert env.reg.get("svc") is not None


def test_an_existing_service_id_is_a_conflict_and_nothing_new_exists(env):
    env.accept(env.body(env.logs("a.log")))
    env.mgr.scan.suggestions += (sugg("sg2"),)
    before = env.snapshot()
    with pytest.raises(Conflict):
        env.accept(env.body(env.logs("b.log")), sid="sg2")
    assert env.snapshot() == before
    # the marker is cleared, a new id works
    env.accept(env.body(env.logs("b.log"), sid="other"), sid="sg2")


def test_unknown_suggestion_raises_not_found(env):
    with pytest.raises(SuggestionNotFound):
        env.accept(env.body(env.logs("a.log")), sid="sg99")


def test_after_accept_the_suggestion_shows_accepted_with_the_service_id(env):
    env.accept(env.body(env.logs("a.log")))
    d = env.mgr.scan.to_dict()["suggestions"][0]
    assert d["state"] == "accepted" and d["service_id"] == "svc"


def test_one_audit_row_discovery_accepted_with_counts_and_the_service_reference(env):
    env.accept(env.body(env.logs("a.log", "b.log")), actor="rest:9.9.9.9")
    rows = [r for r in env.audit() if r["action"] == "discovery_accepted"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "rest:9.9.9.9" and rows[0]["subject"] == "nginx"
    assert rows[0]["detail"] == "paths=2" and rows[0]["service_id"] == "svc"


def test_registry_sink_makes_exactly_one_atomic_call_with_origin_discovery(env, monkeypatch):
    calls = []
    real = env.reg.create_service_with_sources

    async def spy(fields, sources, *, actor, origin="user"):
        calls.append((fields.id, len(sources), actor, origin))
        return await real(fields, sources, actor=actor, origin=origin)
    monkeypatch.setattr(env.reg, "create_service_with_sources", spy)
    env.accept(env.body(env.logs("a.log", "b.log")))
    assert calls == [("svc", 2, "discovery", "discovery")]


def test_a_subscriber_failure_on_the_second_source_rolls_everything_back_and_a_retry_works(env):
    state = {"fail": True, "seen": 0}

    def sub(tx, change):
        if state["fail"] and change.kind == "log_source_added":
            state["seen"] += 1
            if state["seen"] == 2:
                raise RuntimeError("boom")
    env.reg.subscribe(on_service_changed=sub, name="test-sub")
    before = env.snapshot()
    with pytest.raises(SubscriberFailed):
        env.accept(env.body(env.logs("a.log", "b.log")))
    assert env.snapshot() == before and env.reg.list() == []
    state["fail"] = False
    svc, sources = env.accept(env.body(env.logs("a.log", "b.log")))
    assert svc.id == "svc" and len(sources) == 2
