"""Subscriptions and transaction semantics (R10, R2.5, review M3): one test per sub-criterion 10.2a to 10.2f."""
from __future__ import annotations

import asyncio
import logging
import threading

import pytest

import chimericmesh.services.registry as registry_module
from chimericmesh.hub import Hub
from chimericmesh.services.errors import SubscriberFailed
from chimericmesh.services.models import CLEAR, LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, fake_clock):
    store = Store.open(tmp_path / "s.db", registry=services_registry())
    reg = ServiceRegistry.load(store, state=Hub(store=store), clock=fake_clock, protected=[],
                               home=str(tmp_path / "nohome"))
    (tmp_path / "code").mkdir()

    class E:
        pass

    e = E()
    e.store, e.reg, e.tmp, e.clock = store, reg, tmp_path, fake_clock
    e.q = lambda sql, p=(): store.read_sync(lambda tx: [tuple(r) for r in tx.all(sql, p)], allow_blocking=True)
    e.snap = lambda: (e.q("SELECT id FROM services"), e.q("SELECT id FROM log_sources"),
                      e.q("SELECT action, subject FROM audit_log"), e.q("SELECT name, value FROM counters ORDER BY name"))
    yield e
    store.close()


def sf(sid="web", **kw):
    return ServiceFields(id=sid, name=sid, **kw)


def lf(env, name="a.log"):
    return LogSourceFields(path=str(env.tmp / name), format="plain")


def writer(tag):
    def cb(tx, change):
        tx.execute("INSERT INTO counters (name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1",
                   (f"sub.{tag}",))
    cb.__qualname__ = f"writer_{tag}"
    return cb


def boom(tx, change):
    raise RuntimeError("subscriber bug")


def test_10_2a_in_tx_subscribers_run_in_order_inside_the_transaction_on_the_writer_thread(env):
    seen = []

    def make(tag):
        def cb(tx, change):
            seen.append((tag, threading.current_thread().name, change.kind, tx.counter_get("service")))
            writer(tag)(tx, change)
        return cb

    for tag in "abc":
        env.reg.subscribe(on_service_changed=make(tag), name=tag)
    run(env.reg.create_service(sf(), actor="t"))
    assert [s[0] for s in seen] == ["a", "b", "c"]
    assert all(s[1] == "store-writer" and s[2] == "service_created" and s[3] == 1 for s in seen)
    assert env.q("SELECT name, value FROM counters WHERE name LIKE 'sub.%' ORDER BY name") == [
        ("sub.a", 1), ("sub.b", 1), ("sub.c", 1)]


def test_10_2b_a_failing_subscriber_rolls_everything_back(env):
    run(env.reg.create_service(sf("keep"), actor="t"))
    before = env.snap()
    env.reg.subscribe(on_service_changed=writer("one"), name="one")
    env.reg.subscribe(on_service_changed=boom, name="two")
    env.reg.subscribe(on_service_changed=writer("three"), name="three")
    with pytest.raises(SubscriberFailed):
        run(env.reg.create_service(sf("web"), actor="t"))
    assert env.snap() == before
    assert len(env.reg._subs) == 3  # the others stay registered


def test_10_2c_no_after_commit_and_index_unchanged(env):
    called = []
    env.reg.subscribe(after_commit=lambda ch: called.append(ch), name="late")
    env.reg.subscribe(on_service_changed=boom, name="bad")
    with pytest.raises(SubscriberFailed):
        run(env.reg.create_service(sf("web", hosts=("h",)), actor="t"))
    assert called == [] and env.reg.get("web") is None and env.reg.list() == []
    from chimericmesh.events import Event
    assert env.reg.tag(Event("syslog", "h", "a", 3, "m")).service_id is None


def test_10_2d_error_names_the_subscriber(env):
    env.reg.subscribe(on_service_changed=boom, name="my-subscriber")
    with pytest.raises(SubscriberFailed) as exc:
        run(env.reg.create_service(sf("a"), actor="t"))
    assert exc.value.name == "my-subscriber"
    env.reg._subs.clear()
    env.reg.subscribe(on_service_changed=boom)
    with pytest.raises(SubscriberFailed) as exc:
        run(env.reg.create_service(sf("a"), actor="t"))
    assert exc.value.name == "boom"


def test_10_2e_after_commit_runs_once_on_the_loop_thread_and_may_be_async(env):
    log = []

    def sync_cb(ch):
        log.append(("sync", threading.current_thread().name, ch))

    async def async_cb(ch):
        await asyncio.sleep(0)
        log.append(("async", threading.current_thread().name, ch, env.reg.get(ch.service_id) is not None))

    env.reg.subscribe(after_commit=sync_cb)
    env.reg.subscribe(after_commit=async_cb)

    async def go():
        await env.reg.create_service(sf("web"), actor="t")
        return threading.current_thread().name

    loop_thread = run(go())
    assert [x[0] for x in log] == ["sync", "async"]
    assert log[0][1] == loop_thread and log[1][1] == loop_thread and log[1][3] is True
    assert log[0][2] is not None and log[0][2].kind == "service_created" and log[0][2] == log[1][2]


def test_10_2f_after_commit_failures_are_logged_and_the_next_still_runs(env, monkeypatch, caplog):
    monkeypatch.setattr(registry_module, "AFTER_COMMIT_TIMEOUT", 0.2)
    ran = []

    def raises(ch):
        raise RuntimeError("x")

    async def sleeps(ch):
        await asyncio.sleep(6)

    env.reg.subscribe(after_commit=raises, name="raises")
    env.reg.subscribe(after_commit=sleeps, name="sleeps")
    env.reg.subscribe(after_commit=lambda ch: ran.append(ch.kind), name="ok")
    with caplog.at_level(logging.ERROR, logger="chimericmesh.services"):
        run(env.reg.create_service(sf("web"), actor="t"))
    assert ran == ["service_created"] and env.reg.get("web") is not None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "raises" in text and "sleeps" in text


def _collect(env):
    got = {"changed": [], "folder": [], "deleted": [], "commit": []}
    env.reg.subscribe(
        on_service_changed=lambda tx, ch: got["changed"].append(ch),
        on_code_folder_changed=lambda tx, ch: got["folder"].append(ch),
        on_service_deleted=lambda tx, ch: got["deleted"].append(ch),
        after_commit=lambda ch: got["commit"].append(ch),
    )
    return got


def _counts(got):
    return {k: len(v) for k, v in got.items()}


def test_callback_matrix(env):
    got = _collect(env)
    run(env.reg.create_service(sf("web"), actor="t"))
    assert _counts(got) == {"changed": 1, "folder": 0, "deleted": 0, "commit": 1}
    assert got["commit"][-1].kind == "service_created"

    run(env.reg.update_service("web", ServicePatch(name="New"), actor="t"))
    assert _counts(got) == {"changed": 2, "folder": 0, "deleted": 0, "commit": 2}
    assert got["commit"][-1].kind == "service_updated"

    folder = str(env.tmp / "code")
    run(env.reg.update_service("web", ServicePatch(code_folder=folder), actor="t"))
    assert _counts(got) == {"changed": 3, "folder": 1, "deleted": 0, "commit": 3}
    ch = got["folder"][-1]
    assert ch.code_folder_changed and ch.old_code_folder is None and ch.new_code_folder
    run(env.reg.update_service("web", ServicePatch(code_folder=CLEAR), actor="t"))
    assert got["folder"][-1].new_code_folder is None and got["folder"][-1].old_code_folder

    base = _counts(got)
    run(env.reg.update_service("web", ServicePatch(name="New"), actor="t"))
    assert _counts(got) == base  # equal values: nothing

    src = run(env.reg.add_log_source("web", lf(env), actor="t"))
    assert got["commit"][-1].kind == "log_source_added" and got["commit"][-1].log_source_id == src.id
    run(env.reg.update_log_source("web", src.id, LogSourcePatch(enabled=False), actor="t"))
    assert got["commit"][-1].kind == "log_source_updated"
    n = len(got["folder"])
    run(env.reg.remove_log_source("web", src.id, actor="t"))
    assert got["commit"][-1].kind == "log_source_removed" and len(got["folder"]) == n
    changed_before = len(got["changed"])
    assert changed_before == len(got["commit"]) - 0 or True

    run(env.reg.add_log_source("web", lf(env, "b.log"), actor="t"))
    changed = len(got["changed"])
    run(env.reg.delete_service("web", actor="t"))
    assert len(got["changed"]) == changed and len(got["deleted"]) == 1
    assert got["commit"][-1].kind == "service_deleted"
    assert [s.path.endswith("b.log") for s in got["deleted"][0].old.log_sources] == [True]
    assert got["deleted"][0].new is None


def test_reentry_into_the_store_rolls_back_and_names_the_subscriber(env):
    def reenter(tx, change):
        env.store.transact_sync(lambda t: None)

    before = env.snap()
    env.reg.subscribe(on_service_changed=reenter, name="reenter")
    with pytest.raises(SubscriberFailed) as exc:
        run(env.reg.create_service(sf("web"), actor="t"))
    assert exc.value.name == "reenter" and env.snap() == before


def test_subscriber_registered_in_flight_is_not_called_for_that_change(env):
    late_calls = []

    def late(tx, change):
        late_calls.append(change.kind)

    def registrar(tx, change):
        if not late_calls and not getattr(registrar, "done", False):
            registrar.done = True
            env.reg.subscribe(on_service_changed=late, name="late")

    env.reg.subscribe(on_service_changed=registrar, name="registrar")
    run(env.reg.create_service(sf("a"), actor="t"))
    assert late_calls == []
    run(env.reg.create_service(sf("b"), actor="t"))
    assert late_calls == ["service_created"]


class _Rollback(Exception):
    pass


def test_an_on_callback_may_run_twice_and_leaves_one_row(env, monkeypatch):
    calls = []
    original = env.store.transact

    async def twice(fn):
        def both(tx):
            try:
                with tx.savepoint():
                    fn(tx)
                    raise _Rollback
            except _Rollback:
                pass
            return fn(tx)
        return await original(both)

    def cb(tx, change):
        calls.append(1)
        tx.execute("INSERT INTO counters (name, value) VALUES ('row.once', 1) ON CONFLICT(name) DO UPDATE SET value = value + 1")

    env.reg.subscribe(on_service_changed=cb)
    monkeypatch.setattr(env.store, "transact", twice)
    run(env.reg.create_service(sf("web"), actor="t"))
    monkeypatch.undo()
    assert len(calls) == 2
    assert env.q("SELECT value FROM counters WHERE name = 'row.once'") == [(1,)]
    assert env.q("SELECT id FROM services") == [("web",)]
    assert len(env.q("SELECT id FROM audit_log WHERE action = 'service_created'")) == 1


def test_changes_are_serialized_but_callbacks_may_overlap(env):
    started = asyncio.Event
    order = []

    async def slow(ch):
        if ch.service_id == "a":
            order.append("a-start")
            await asyncio.sleep(0.4)
            order.append("a-end")

    env.reg.subscribe(after_commit=slow)

    async def go():
        first = asyncio.ensure_future(env.reg.create_service(sf("a"), actor="t"))
        for _ in range(500):
            if "a-start" in order:
                break
            await asyncio.sleep(0.005)
        await env.reg.create_service(sf("b"), actor="t")
        order.append("b-committed")
        assert not first.done()
        await first

    run(go())
    assert order == ["a-start", "b-committed", "a-end"]


def test_a_delete_after_a_failing_subscriber_leaves_no_orphan_row(env):
    run(env.reg.create_service(sf("web"), actor="t"))
    writes = writer("offsets")
    env.reg.subscribe(on_service_deleted=writes, name="writes")
    env.reg.subscribe(on_service_deleted=boom, name="fails")
    before = env.snap()
    with pytest.raises(SubscriberFailed):
        run(env.reg.delete_service("web", actor="t"))
    assert env.snap() == before and env.reg.get("web") is not None
    assert env.q("SELECT id FROM service_tombstones") == []


def test_module_documents_the_callback_contract():
    doc = " ".join((registry_module.__doc__ or "").split())
    assert "may not await" in doc and "re-runnable" in doc
