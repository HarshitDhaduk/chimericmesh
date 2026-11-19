"""ServiceRegistry: load, create, atomic create, edit, delete, log sources, injection (R1 to R4, R10).

Every test runs against a plain ``Hub()`` (registry on a private ``:memory:`` store) and a
store-backed ``Hub(store=...)`` (review M2).
"""
from __future__ import annotations

import asyncio
import builtins
import json
import logging
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.errors import (
    BuiltinService,
    Conflict,
    LogSourceNotFound,
    ServiceNotFound,
    ServicesNotMigrated,
    SubscriberFailed,
    ValidationFailed,
)
from chimericmesh.services.models import CLEAR, LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store, StoreWriteError
from chimericmesh.store.schema import core_registry


@dataclass
class Env:
    store: Store
    hub: Hub
    reg: ServiceRegistry
    tmp: object
    clock: object
    kind: str

    def folder(self, name="proj"):
        p = self.tmp / name
        p.mkdir(exist_ok=True)
        return str(p)

    def q(self, sql, params=()):
        return self.store.read_sync(lambda tx: [tuple(r) for r in tx.all(sql, params)], allow_blocking=True)

    def count(self, table):
        return self.q(f"SELECT COUNT(*) FROM {table}")[0][0]

    def snap(self):
        return {t: self.count(t) for t in ("services", "log_sources", "audit_log", "service_tombstones")} | {
            "counters": self.q("SELECT name, value FROM counters ORDER BY name")}

    def raw(self, sql, params=()):
        self.store.transact_sync(lambda tx: tx.execute(sql, params), allow_blocking=True)


@pytest.fixture(params=["plain", "store"])
def env(request, tmp_path, fake_clock):
    if request.param == "plain":
        store = Store.open(":memory:", registry=services_registry())
        hub = Hub()
    else:
        store = Store.open(tmp_path / "s.db", registry=services_registry())
        hub = Hub(store=store)
    home = tmp_path / "home"
    home.mkdir()
    reg = ServiceRegistry.load(store, state=hub, protected=[str(home / ".chimericmesh")], clock=fake_clock,
                               home=str(home))
    yield Env(store, hub, reg, tmp_path, fake_clock, request.param)
    store.close()


def run(coro):
    return asyncio.run(coro)


def sf(sid="web", **kw):
    return ServiceFields(id=sid, name=kw.pop("name", sid.title()), **kw)


def lf(path, fmt="plain", **kw):
    return LogSourceFields(path=path, format=fmt, **kw)


def audit(env):
    return run(env.store.audit.read(limit=500))


# ---- 3.2 load and create --------------------------------------------------------------------

def test_load_empty(env):
    assert env.reg.list() == []
    assert env.reg.log_sources() == []


def test_create_service_returns_service_and_writes_audit(env):
    svc = run(env.reg.create_service(sf("web", hosts=("h1",), app_names=("nginx",)), actor="rest:1.2.3.4"))
    assert svc.id == "web" and svc.seq == 1
    assert svc.created_at == svc.updated_at == env.clock()
    assert env.reg.get("web") == svc and env.reg.list() == [svc]
    rows = [r for r in audit(env) if r["action"] == "service_created"]
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "rest:1.2.3.4" and row["subject"] == "web" and row["service_id"] == "web"
    assert "h1" not in row["detail"] and "nginx" not in row["detail"]
    assert "hosts" in row["detail"] and "name" in row["detail"]


def test_registry_survives_reopen(tmp_path):
    path = tmp_path / "r.db"
    store = Store.open(path, registry=services_registry())
    hub = Hub(store=store)
    reg = ServiceRegistry.load(store, state=hub)
    run(reg.create_service(sf("a"), actor="t"))
    run(reg.create_service(sf("b"), actor="t"))
    run(reg.add_log_source("a", lf(str(tmp_path / "a.log")), actor="t"))
    before = reg.list()
    store.close()
    store = Store.open(path, registry=services_registry())
    try:
        reg2 = ServiceRegistry.load(store, state=Hub(store=store))
        assert reg2.list() == before
        assert [s.id for s in reg2.list()] == ["a", "b"]
    finally:
        store.close()


def test_memory_store_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store.open(":memory:", registry=services_registry())
    try:
        reg = ServiceRegistry.load(store, state=Hub())
        run(reg.create_service(sf("a"), actor="t"))
        assert store.path == ":memory:"
        assert not list(tmp_path.glob("*.db*"))
    finally:
        store.close()


def test_duplicate_id_is_a_conflict(env):
    run(env.reg.create_service(sf("web"), actor="t"))
    with pytest.raises(Conflict):
        run(env.reg.create_service(sf("web"), actor="t"))
    assert len(env.reg.list()) == 1


def test_101st_service_is_refused(env):
    for i in range(100):
        env.raw("INSERT INTO services (id, seq, name, created_at, updated_at) VALUES (?,?,?,0,0)",
                (f"s{i}", i + 1, "n"))
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.create_service(sf("one-more"), actor="t"))
    assert exc.value.field == "services"


def test_invalid_fields_are_refused_before_the_transaction(env):
    with pytest.raises(ValidationFailed):
        run(env.reg.create_service(ServiceFields(id="Bad Id", name="x"), actor="t"))
    with pytest.raises(ValidationFailed):
        run(env.reg.create_service(ServiceFields(id="ok", name="x", protocols=("gopher",)), actor="t"))
    assert env.count("audit_log") == 0


def test_two_concurrent_creates_with_one_id(env):
    async def go():
        return await asyncio.gather(
            env.reg.create_service(sf("same"), actor="a"),
            env.reg.create_service(sf("same"), actor="b"),
            return_exceptions=True,
        )

    results = run(go())
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    assert sum(1 for r in results if isinstance(r, Conflict)) == 1


def test_cancelled_caller_still_applies_the_delta(env, caplog):
    async def go():
        task = asyncio.ensure_future(env.reg.create_service(sf("late"), actor="t"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(200):
            if env.reg.get("late") is not None:
                break
            await asyncio.sleep(0.01)

    with caplog.at_level(logging.DEBUG):
        run(go())
    assert env.reg.get("late") is not None
    assert not [r for r in caplog.records if "InvalidStateError" in r.getMessage()]


def test_load_on_store_without_migrations_raises(tmp_path):
    store = Store.open(tmp_path / "core.db", registry=core_registry())
    try:
        with pytest.raises(ServicesNotMigrated):
            ServiceRegistry.load(store, state=Hub())
    finally:
        store.close()


# ---- 3.2a atomic create with sources ---------------------------------------------------------

def test_atomic_create_with_three_sources(env):
    changes, committed = [], []
    env.reg.subscribe(on_service_changed=lambda tx, ch: changes.append(ch.kind),
                      after_commit=lambda ch: committed.append(ch.kind))
    paths = [str(env.tmp / f"l{i}.log") for i in range(3)]
    svc, sources = run(env.reg.create_service_with_sources(
        sf("web"), [lf(p) for p in paths], actor="discovery", origin="discovery"))
    assert [s.id for s in sources] == ["ls1", "ls2", "ls3"]
    assert all(s.origin == "discovery" for s in sources)
    assert svc.log_sources == tuple(sources)
    kinds = [r["action"] for r in reversed(audit(env))]
    assert kinds == ["service_created"] + ["log_source_added"] * 3
    expected = ["service_created"] + ["log_source_added"] * 3
    assert changes == expected and committed == expected


def test_atomic_create_with_no_sources_equals_create(env):
    svc, sources = run(env.reg.create_service_with_sources(sf("web"), [], actor="t"))
    assert sources == [] and env.reg.get("web") == svc


def _snapshot_unchanged(env, call):
    before = env.snap()
    with pytest.raises(Exception) as exc:
        call()
    assert env.snap() == before
    assert env.reg.list() == []
    return exc.value


def test_atomic_create_all_or_nothing(env, monkeypatch):
    good = str(env.tmp / "ok.log")
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))

    def make(sources, fields=None, actor="t"):
        return lambda: run(env.reg.create_service_with_sources(fields or sf("web"), sources, actor=actor))

    bad_cases = {
        "bad format on third": [lf(good), lf(good), lf(good, "nope")],
        "directory": [lf(good), lf(str(env.tmp))],
        "relative": [lf(good), lf("rel.log")],
        "21 sources": [lf(str(env.tmp / f"{i}.log")) for i in range(21)],
    }
    for name, sources in bad_cases.items():
        _snapshot_unchanged(env, make(sources))
    # duplicate and tombstoned ids
    run(env.reg.create_service(sf("dup"), actor="t"))
    run(env.reg.create_service(sf("gone"), actor="t"))
    run(env.reg.delete_service("gone", actor="t"))
    base = env.snap()
    for sid in ("dup", "gone"):
        with pytest.raises(Conflict):
            make([lf(good)], sf(sid))()
        assert env.snap() == base
    # a raising subscriber
    def boom(tx, ch):
        raise RuntimeError("no")

    env.reg.subscribe(on_service_changed=boom, name="boom")
    with pytest.raises(SubscriberFailed):
        make([lf(good)], sf("sub"))()
    assert env.snap() == base
    env.reg._subs.clear()
    # an injected store failure
    async def fail(fn):
        raise StoreWriteError("down")

    monkeypatch.setattr(env.store, "transact", fail)
    with pytest.raises(StoreWriteError):
        make([lf(good)], sf("down"))()
    monkeypatch.undo()
    assert env.snap() == base
    # no id was burned
    _, srcs = run(env.reg.create_service_with_sources(sf("fine"), [lf(good)], actor="t"))
    assert [s.id for s in srcs] == ["ls1"]
    assert not [a for a in opened if str(a[0]).endswith(".log")]


def test_atomic_create_caps_total_sources_and_services(env):
    for i in range(10):
        run(env.reg.create_service_with_sources(
            sf(f"s{i}"), [lf(str(env.tmp / f"{i}-{j}.log")) for j in range(20)], actor="t"))
    before = env.snap()
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.create_service_with_sources(sf("x"), [lf(str(env.tmp / "x.log"))], actor="t"))
    assert exc.value.field == "log_sources"
    assert env.snap() == before


def test_atomic_create_101st_service(env):
    for i in range(100):
        env.raw("INSERT INTO services (id, seq, name, created_at, updated_at) VALUES (?,?,?,0,0)", (f"s{i}", i + 1, "n"))
    before = env.snap()
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.create_service_with_sources(sf("x"), [lf(str(env.tmp / "x.log"))], actor="t"))
    assert exc.value.field == "services"
    assert env.snap() == before


def test_registry_normalizes_code_folder_spellings(env):
    folder = env.folder()
    spelled = os.path.join(os.path.dirname(folder), ".", os.path.basename(folder)) + os.sep
    a = run(env.reg.create_service(sf("a", code_folder=spelled), actor="t"))
    assert a.code_folder == os.path.realpath(folder)
    other = env.folder("other")
    run(env.reg.update_service("a", ServicePatch(code_folder=other + os.sep + "."), actor="t"))
    assert env.reg.get("a").code_folder == os.path.realpath(other)
    b, _ = run(env.reg.create_service_with_sources(sf("b", code_folder=other + os.sep + os.sep), [], actor="t"))
    assert b.code_folder == os.path.realpath(other)
    again = run(env.reg.create_service(sf("c", code_folder=b.code_folder), actor="t"))
    assert again.code_folder == b.code_folder


# ---- 3.3 edit and delete ---------------------------------------------------------------------

def test_update_changes_only_given_fields(env):
    run(env.reg.create_service(sf("web", hosts=("h",), app_names=("a",)), actor="t"))
    env.clock.advance(5)
    new = run(env.reg.update_service("web", ServicePatch(name="Renamed"), actor="t"))
    assert new.name == "Renamed" and new.hosts == ("h",) and new.app_names == ("a",)
    assert new.updated_at > new.created_at
    row = [r for r in audit(env) if r["action"] == "service_updated"][0]
    assert row["detail"] == "fields=name"


def test_update_errors(env):
    with pytest.raises(ServiceNotFound):
        run(env.reg.update_service("nope", ServicePatch(name="x"), actor="t"))
    with pytest.raises(BuiltinService):
        run(env.reg.update_service("unassigned", ServicePatch(name="x"), actor="t"))
    with pytest.raises(BuiltinService):
        run(env.reg.delete_service("unassigned", actor="t"))
    run(env.reg.create_service(sf("web"), actor="t"))
    f = env.tmp / "afile"
    f.write_text("x")
    for bad in (str(f), str(env.tmp / "missing"), "relative", str(env.tmp.parent.parent), os.path.abspath(os.sep)):
        with pytest.raises(ValidationFailed) as exc:
            run(env.reg.update_service("web", ServicePatch(code_folder=bad), actor="t"))
        assert exc.value.field == "code_folder"


def test_code_folder_set_audit_and_clear(env):
    run(env.reg.create_service(sf("web"), actor="t"))
    assert env.reg.get("web").code_folder is None  # no auto-approvable scope (R2.4)
    folder = env.folder()
    svc = run(env.reg.update_service("web", ServicePatch(code_folder=folder), actor="t"))
    assert svc.code_folder == os.path.realpath(folder)
    row = [r for r in audit(env) if r["action"] == "service_updated"][0]
    assert "code_folder" in row["detail"] and "None -> " in row["detail"]
    svc = run(env.reg.update_service("web", ServicePatch(code_folder=CLEAR), actor="t"))
    assert svc.code_folder is None
    assert env.reg.get("web").code_folder is None


def test_edit_with_equal_values_writes_nothing(env):
    run(env.reg.create_service(sf("web", hosts=("h",)), actor="t"))
    before = env.snap()
    stored = env.reg.get("web")
    env.clock.advance(10)
    calls = []
    env.reg.subscribe(on_service_changed=lambda tx, ch: calls.append(ch), after_commit=lambda ch: calls.append(ch))
    got = run(env.reg.update_service("web", ServicePatch(name=stored.name, hosts=("h",)), actor="t"))
    assert got == stored and got.updated_at == stored.updated_at
    assert env.snap() == before and calls == []


def test_delete_service(env):
    run(env.reg.create_service_with_sources(sf("web"), [lf(str(env.tmp / "a.log"))], actor="t"))
    env.raw("INSERT INTO events (ts, source, host, app, severity, message, service_id, ingested)"
            " VALUES (0,'s','h','a',3,'m','web',0)")
    env.raw("INSERT INTO incidents (id, host, service_id, category, signature, severity, count, first_seen, last_seen, sample)"
            " VALUES ('i1','h','web','c','s',3,1,0,0,'x')")
    env.raw("INSERT INTO proposals (id, seq, incident_id, action, status, created, service_id)"
            " VALUES ('p1',1,'i1','fix','pending',0,'web')")
    run(env.reg.delete_service("web", actor="t"))
    assert env.reg.get("web") is None and env.reg.list() == [] and env.reg.log_sources() == []
    assert env.count("log_sources") == 0
    assert env.q("SELECT id FROM service_tombstones") == [("web",)]
    for table in ("events", "incidents", "proposals"):
        assert env.q(f"SELECT COUNT(*) FROM {table} WHERE service_id = 'web'")[0][0] == 1
    assert [r["action"] for r in audit(env)][0] == "service_deleted"
    with pytest.raises(Conflict):
        run(env.reg.create_service(sf("web"), actor="t"))
    run(env.reg.create_service(sf("web2"), actor="t"))


def test_tombstone_survives_restart_and_a_racing_create(tmp_path):
    path = tmp_path / "t.db"
    store = Store.open(path, registry=services_registry())
    reg = ServiceRegistry.load(store, state=Hub(store=store))
    run(reg.create_service(sf("a"), actor="t"))

    async def race():
        return await asyncio.gather(reg.delete_service("a", actor="t"), reg.create_service(sf("a"), actor="t"),
                                    return_exceptions=True)

    first, second = run(race())
    assert first is None and isinstance(second, Conflict)
    store.close()
    store = Store.open(path, registry=services_registry())
    try:
        reg = ServiceRegistry.load(store, state=Hub(store=store))
        with pytest.raises(Conflict):
            run(reg.create_service(sf("a"), actor="t"))
    finally:
        store.close()


def test_stored_events_are_not_rewritten_and_new_ones_are_unassigned(env):
    run(env.reg.create_service(sf("web", hosts=("h",)), actor="t"))
    env.raw("INSERT INTO events (ts, source, host, app, severity, message, service_id, ingested)"
            " VALUES (0,'s','h','a',3,'m','web',0)")
    ev = Event("syslog", "h", "x", 3, "m")
    assert env.reg.tag(ev).service_id == "web"
    run(env.reg.delete_service("web", actor="t"))
    assert env.reg.tag(ev).service_id is None
    assert env.q("SELECT service_id FROM events") == [("web",)]


def test_creation_order_decides_after_the_first_is_deleted(env):
    run(env.reg.create_service(sf("a", hosts=("h",)), actor="t"))
    run(env.reg.create_service(sf("b", hosts=("h",)), actor="t"))
    ev = Event("syslog", "h", "x", 3, "m")
    assert env.reg.tag(ev).service_id == "a"
    run(env.reg.delete_service("a", actor="t"))
    assert env.reg.tag(ev).service_id == "b"


def test_tag_changes_only_service_id(env):
    run(env.reg.create_service(sf("web", app_names=("nginx",)), actor="t"))
    ev = Event("syslog", "h", "NGINX", 3, "m", raw="r", service_id="stale", log_source_id=None)
    tagged = env.reg.tag(ev)
    assert tagged.service_id == "web"
    assert {**tagged.to_dict(), "service_id": None} == {**ev.to_dict(), "service_id": None}
    same = Event("syslog", "h", "NGINX", 3, "m", service_id="web")
    assert env.reg.tag(same) is same


# ---- 3.4 log sources -------------------------------------------------------------------------

def _svc(env, sid="web"):
    return run(env.reg.create_service(sf(sid), actor="t"))


def test_add_log_source(env, monkeypatch):
    _svc(env)
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")) if str(a[0]).endswith(".log") else real_open(*a, **k))
    src = run(env.reg.add_log_source("web", lf(str(env.tmp / "missing.log"), backfill_lines=7, host="box"), actor="t"))
    assert src.id == "ls1" and src.backfill_lines == 7 and src.host == "box" and src.origin == "user"
    assert env.reg.source("ls1") == src and env.reg.log_sources() == [src]
    assert env.reg.get("web").log_sources == (src,)
    assert env.reg.tag(Event("api", "h", "a", 3, "m", log_source_id="ls1")).service_id == "web"
    src2 = run(env.reg.add_log_source("web", lf(str(env.tmp / "b.log")), actor="t", origin="discovery"))
    assert src2.origin == "discovery" and src2.backfill_lines == 0
    assert src2.id == "ls2"


def test_add_log_source_errors(env):
    _svc(env)
    for fields in (lf(str(env.tmp)), lf("rel.log"), lf(str(env.tmp / "a\x00.log")), lf(str(env.tmp / "a.log"), "nope")):
        with pytest.raises(ValidationFailed):
            run(env.reg.add_log_source("web", fields, actor="t"))
    with pytest.raises(ServiceNotFound):
        run(env.reg.add_log_source("nope", lf(str(env.tmp / "a.log")), actor="t"))
    with pytest.raises(BuiltinService):
        run(env.reg.add_log_source("unassigned", lf(str(env.tmp / "a.log")), actor="t"))


def test_known_formats_apply_to_later_writes_only(env):
    _svc(env)
    first = run(env.reg.add_log_source("web", lf(str(env.tmp / "a.log"), "xferlog"), actor="t"))
    env.reg.set_known_formats(["Plain"])
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.add_log_source("web", lf(str(env.tmp / "b.log"), "xferlog"), actor="t"))
    assert exc.value.field == "format"
    run(env.reg.add_log_source("web", lf(str(env.tmp / "b.log"), "plain"), actor="t"))
    assert env.reg.source(first.id).format == "xferlog"


def test_ids_are_never_reused_across_delete_and_restart(tmp_path):
    path = tmp_path / "ids.db"
    store = Store.open(path, registry=services_registry())
    reg = ServiceRegistry.load(store, state=Hub(store=store))
    run(reg.create_service(sf("web"), actor="t"))
    a = run(reg.add_log_source("web", lf(str(tmp_path / "a.log")), actor="t"))
    run(reg.remove_log_source("web", a.id, actor="t"))
    store.close()
    store = Store.open(path, registry=services_registry())
    try:
        reg = ServiceRegistry.load(store, state=Hub(store=store))
        b = run(reg.add_log_source("web", lf(str(tmp_path / "b.log")), actor="t"))
        assert (a.id, b.id) == ("ls1", "ls2")
    finally:
        store.close()


def test_source_caps(env):
    _svc(env)
    for i in range(20):
        run(env.reg.add_log_source("web", lf(str(env.tmp / f"{i}.log")), actor="t"))
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.add_log_source("web", lf(str(env.tmp / "21.log")), actor="t"))
    assert exc.value.field == "log_sources"
    for i in range(9):
        run(env.reg.create_service_with_sources(
            sf(f"s{i}"), [lf(str(env.tmp / f"{i}-{j}.log")) for j in range(20)], actor="t"))
    run(env.reg.create_service(sf("last"), actor="t"))
    with pytest.raises(ValidationFailed) as exc:
        run(env.reg.add_log_source("last", lf(str(env.tmp / "z.log")), actor="t"))
    assert exc.value.field == "log_sources"


def test_same_path_twice_is_accepted(env):
    _svc(env)
    p = str(env.tmp / "same.log")
    run(env.reg.add_log_source("web", lf(p), actor="t"))
    run(env.reg.add_log_source("web", lf(p), actor="t"))
    assert len(env.reg.log_sources()) == 2


def test_update_log_source(env):
    _svc(env)
    src = run(env.reg.add_log_source("web", lf(str(env.tmp / "a.log"), host="h"), actor="t"))
    env.clock.advance(3)
    upd = run(env.reg.update_log_source(
        "web", src.id, LogSourcePatch(format="ncsa", enabled=False, host=CLEAR, backfill_lines=9), actor="t"))
    assert (upd.format, upd.enabled, upd.host, upd.backfill_lines) == ("ncsa", False, None, 9)
    assert upd.updated_at > src.updated_at and env.reg.source(src.id) == upd
    assert env.reg.log_sources(enabled_only=True) == []
    row = [r for r in audit(env) if r["action"] == "log_source_updated"][0]
    assert row["subject"] == src.id and set(row["detail"].removeprefix("fields=").split(",")) == {
        "format", "enabled", "host", "backfill_lines"}
    with pytest.raises(LogSourceNotFound):
        run(env.reg.update_log_source("web", "ls99", LogSourcePatch(enabled=True), actor="t"))
    with pytest.raises(ValidationFailed):
        run(env.reg.update_log_source("web", src.id, LogSourcePatch(format="bogus"), actor="t"))


def test_equal_source_update_writes_nothing(env):
    _svc(env)
    src = run(env.reg.add_log_source("web", lf(str(env.tmp / "a.log")), actor="t"))
    calls = []
    env.reg.subscribe(on_service_changed=lambda tx, ch: calls.append(1), after_commit=lambda ch: calls.append(2))
    before = env.snap()
    got = run(env.reg.update_log_source("web", src.id, LogSourcePatch(format="plain", enabled=True), actor="t"))
    assert got == src and calls == [] and env.snap() == before


def test_remove_log_source(env):
    _svc(env)
    src = run(env.reg.add_log_source("web", lf(str(env.tmp / "a.log")), actor="t"))
    run(env.reg.remove_log_source("web", src.id, actor="t"))
    assert env.reg.source(src.id) is None and env.reg.get("web").log_sources == ()
    assert [r["action"] for r in audit(env)][0] == "log_source_removed"
    with pytest.raises(LogSourceNotFound):
        run(env.reg.remove_log_source("web", src.id, actor="t"))


def test_enabled_only_and_status_provider_untouched(env):
    _svc(env)
    called = []
    env.reg.set_status_provider(lambda sid: called.append(sid))
    a = run(env.reg.add_log_source("web", lf(str(env.tmp / "a.log")), actor="t"))
    run(env.reg.add_log_source("web", lf(str(env.tmp / "b.log"), enabled=False), actor="t"))
    assert [s.id for s in env.reg.log_sources(enabled_only=True)] == [a.id]
    assert len(env.reg.log_sources()) == 2 and called == []


# ---- 3.6 injection points ---------------------------------------------------------------------

def test_set_known_formats_validation(env):
    env.reg.set_known_formats(["Plain", "NCSA"])
    assert env.reg._known_formats == {"plain", "ncsa"}
    for bad in ("Bad Name", "", "a" * 31, "-x", "a_b"):
        with pytest.raises(ValueError):
            env.reg.set_known_formats([bad])
    assert env.reg._known_formats == {"plain", "ncsa"}


def test_set_status_provider_and_removal(env):
    fn = lambda sid: None  # noqa: E731
    env.reg.set_status_provider(fn)
    assert env.reg._status_provider is fn
    env.reg.set_status_provider(None)
    assert env.reg._status_provider is None


def test_lists_are_in_creation_order(env):
    for sid in ("zeta", "alpha", "mid"):
        _svc(env, sid)
    assert [s.id for s in env.reg.list()] == ["zeta", "alpha", "mid"]
    run(env.reg.add_log_source("alpha", lf(str(env.tmp / "1.log")), actor="t"))
    run(env.reg.add_log_source("zeta", lf(str(env.tmp / "2.log")), actor="t"))
    assert [s.id for s in env.reg.log_sources()] == ["ls1", "ls2"]


def test_importing_services_loads_no_web_framework_or_connectors():
    code = textwrap.dedent("""
        import sys, chimericmesh.services
        bad = [m for m in sys.modules if m.split('.')[0] in ('fastapi', 'starlette', 'pydantic')
               or m.startswith('chimericmesh.connectors')]
        print(json_bad := bad)
        raise SystemExit(1 if bad else 0)
    """)
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.path.abspath(src)})
    assert out.returncode == 0, out.stdout + out.stderr


def test_registry_creates_no_task_or_thread(env):
    import threading

    async def go():
        before = len(asyncio.all_tasks())
        await env.reg.create_service(sf("web"), actor="t")
        await env.reg.add_log_source("web", lf(str(env.tmp / "a.log")), actor="t")
        return before, len(asyncio.all_tasks())

    threads = threading.active_count()
    before, after = run(go())
    assert before == after and threading.active_count() == threads
