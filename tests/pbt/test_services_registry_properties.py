"""Properties 17 (subscriber atomicity) and 18 (registry round trip)."""
from __future__ import annotations

import asyncio
import os
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.hub import Hub
from chimericmesh.services.errors import ServicesError, SubscriberFailed
from chimericmesh.services.models import LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store


def _q(store, sql, params=()):
    return store.read_sync(lambda tx: [tuple(r) for r in tx.all(sql, params)], allow_blocking=True)


def _snapshot(store):
    return (_q(store, "SELECT * FROM services"), _q(store, "SELECT * FROM log_sources"),
            _q(store, "SELECT action, subject, detail FROM audit_log"), _q(store, "SELECT * FROM counters ORDER BY name"),
            _q(store, "SELECT * FROM service_tombstones"))


def _writer(i):
    def cb(tx, change):
        tx.execute("INSERT INTO counters (name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1",
                   (f"p.sub{i}",))
    return cb


def _failer(tx, change):
    raise RuntimeError("boom")


KINDS = ["create", "update", "delete", "add_source", "update_source", "remove_source", "create_with_sources"]


@settings(max_examples=40, deadline=None)
@given(n=st.integers(1, 5), data=st.data(), kind=st.sampled_from(KINDS))
def test_property_17_subscriber_atomicity(n, data, kind):
    k = data.draw(st.integers(0, n - 1))
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(os.path.join(tmp, "p.db"), registry=services_registry())
        try:
            reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
            log = os.path.join(tmp, "a.log")

            async def setup():
                await reg.create_service(ServiceFields(id="web", name="web"), actor="t")
                await reg.add_log_source("web", LogSourceFields(path=log, format="plain"), actor="t")

            asyncio.run(setup())
            committed: list[object] = []
            for i in range(n):
                fn = _failer if i == k else _writer(i)
                reg.subscribe(on_service_changed=fn, on_service_deleted=fn, after_commit=committed.append, name=f"s{i}")
            before_db, before_list = _snapshot(store), reg.list()

            async def mutate():
                if kind == "create":
                    await reg.create_service(ServiceFields(id="new", name="new"), actor="t")
                elif kind == "create_with_sources":
                    await reg.create_service_with_sources(
                        ServiceFields(id="new", name="new"), [LogSourceFields(path=os.path.join(tmp, "n.log"), format="plain")],
                        actor="t")
                elif kind == "update":
                    await reg.update_service("web", ServicePatch(name="changed"), actor="t")
                elif kind == "delete":
                    await reg.delete_service("web", actor="t")
                elif kind == "add_source":
                    await reg.add_log_source("web", LogSourceFields(path=os.path.join(tmp, "b.log"), format="plain"), actor="t")
                elif kind == "update_source":
                    await reg.update_log_source("web", "ls1", LogSourcePatch(enabled=False), actor="t")
                else:
                    await reg.remove_log_source("web", "ls1", actor="t")

            try:
                asyncio.run(mutate())
                raise AssertionError("expected SubscriberFailed")
            except SubscriberFailed as exc:
                assert exc.name == f"s{k}"
            assert _snapshot(store) == before_db
            assert reg.list() == before_list
            assert committed == []
        finally:
            store.close()


@st.composite
def op_lists(draw):
    ops = []
    for _ in range(draw(st.integers(1, 14))):
        sid = draw(st.sampled_from(["a", "b", "c"]))
        ops.append((draw(st.sampled_from(["create", "rename", "delete", "add", "remove", "restart"])), sid,
                    draw(st.integers(0, 3))))
    return ops


@settings(max_examples=30, deadline=None)
@given(op_lists())
def test_property_18_registry_round_trip(ops):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.db")
        store = Store.open(path, registry=services_registry())
        reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
        issued: list[str] = []
        try:
            async def go(op, sid, n):
                try:
                    if op == "create":
                        await reg.create_service(ServiceFields(id=sid, name=sid.upper()), actor="t")
                    elif op == "rename":
                        await reg.update_service(sid, ServicePatch(name=f"{sid}-{n}"), actor="t")
                    elif op == "delete":
                        await reg.delete_service(sid, actor="t")
                    elif op == "add":
                        src = await reg.add_log_source(sid, LogSourceFields(path=os.path.join(tmp, f"{n}.log"), format="plain"),
                                                       actor="t")
                        assert src.id not in issued
                        issued.append(src.id)
                    elif op == "remove":
                        svc = reg.get(sid)
                        if svc and svc.log_sources:
                            await reg.remove_log_source(sid, svc.log_sources[-1].id, actor="t")
                except ServicesError:
                    pass

            for op, sid, n in ops:
                if op == "restart":
                    before = reg.list()
                    store.close()
                    store = Store.open(path, registry=services_registry())
                    reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
                    assert reg.list() == before
                else:
                    asyncio.run(go(op, sid, n))
            before = reg.list()
            ids = [s.id for s in before]
            assert len(ids) == len(set(ids))
            store.close()
            store = Store.open(path, registry=services_registry())
            reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
            assert reg.list() == before
            assert [s.seq for s in reg.list()] == sorted(s.seq for s in reg.list())
            # a log source id is never reused, even after a restart
            if reg.list():
                src = asyncio.run(reg.add_log_source(reg.list()[0].id, LogSourceFields(path=os.path.join(tmp, "z.log"), format="plain"),
                                                     actor="t"))
                assert src.id not in issued
        finally:
            store.close()
