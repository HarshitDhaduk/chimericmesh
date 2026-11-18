import asyncio
import sqlite3

import pytest

from chimericmesh.store.audit import AuditLog
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store


def open_store(now=None) -> Store:
    kwargs = {"now": now} if now else {}
    return Store.open(":memory:", registry=core_registry(), **kwargs)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store():
    s = open_store()
    yield s
    s.close()


def all_rows(store: Store):
    return run(store.audit.read(limit=500))


def test_ids_increase_and_timestamp_comes_from_the_store_clock(fake_clock):
    s = open_store(fake_clock)
    try:
        a = run(s.audit.append("alice", "approve", "p1", "ok"))
        fake_clock.advance(5)
        b = run(s.audit.append("alice", "approve", "p2"))
        assert b > a
        rows = all_rows(s)
        assert [r["id"] for r in rows] == [b, a]
        assert rows[1]["ts"] == 1_000_000.0 and rows[0]["ts"] == 1_000_005.0
        assert rows[1]["detail"] == "ok" and rows[0]["detail"] == ""
    finally:
        s.close()


def test_read_is_newest_first_with_limit_and_before(store):
    ids = [run(store.audit.append("a", "act", f"s{i}")) for i in range(5)]
    assert [r["id"] for r in run(store.audit.read(limit=3))] == ids[::-1][:3]
    assert [r["id"] for r in run(store.audit.read(before=ids[3]))] == [ids[2], ids[1], ids[0]]
    assert [r["id"] for r in run(store.audit.read(limit=2, before=ids[3]))] == [ids[2], ids[1]]


def test_read_default_limit_is_100_and_clamped_to_500(store):
    def fill(tx):
        for i in range(600):
            store.audit.append_tx(tx, "a", "act", f"s{i}")

    store.transact_sync(fill)
    assert len(run(store.audit.read())) == 100
    assert len(run(store.audit.read(limit=10_000))) == 500
    assert len(run(store.audit.read(limit=0))) == 1
    assert len(run(store.audit.read(limit=-5))) == 1


def test_read_filters_by_reference_columns(store):
    run(store.audit.append("a", "x", "s", service_id="svc1"))
    run(store.audit.append("a", "x", "s", rule_id="r1"))
    run(store.audit.append("a", "x", "s", proposal_id="p1", service_id="svc2"))
    assert len(run(store.audit.read(service_id="svc1"))) == 1
    assert len(run(store.audit.read(rule_id="r1"))) == 1
    rows = run(store.audit.read(proposal_id="p1"))
    assert len(rows) == 1 and rows[0]["service_id"] == "svc2"


def test_caps_and_control_character_stripping(store):
    run(store.audit.append("a" * 300, "b" * 300, "c" * 300, "d" * 3000,
                           service_id="s" * 300, rule_id="r" * 300, proposal_id="p" * 300))
    row = all_rows(store)[0]
    assert (len(row["actor"]), len(row["action"]), len(row["subject"])) == (100, 100, 100)
    assert len(row["detail"]) == 1000
    assert (len(row["service_id"]), len(row["rule_id"]), len(row["proposal_id"])) == (100, 100, 100)

    run(store.audit.append("a\x00\r\nb", "ac\x1btion", "su\x7fb\x9fj", "line1\nline2\x00end"))
    row = all_rows(store)[0]
    assert row["actor"] == "ab" and row["action"] == "action" and row["subject"] == "subj"
    assert row["detail"] == "line1line2end"


def test_lone_surrogates_are_replaced_and_never_raise(store):
    run(store.audit.append("a\ud800", "x", "s", "d\udfff"))
    row = all_rows(store)[0]
    assert "\ud800" not in row["actor"] and "\udfff" not in row["detail"]


def test_secrets_and_url_credentials_never_reach_detail(store):
    store.add_redaction("TOKEN-CANARY-123")
    run(store.audit.append(
        "a", "x", "s",
        "token=TOKEN-CANARY-123 url=https://bob:hunter2@example.com/hook and ftp://admin:pw@h/x",
    ))
    detail = all_rows(store)[0]["detail"]
    assert "TOKEN-CANARY-123" not in detail
    assert "hunter2" not in detail and "bob" not in detail and "admin" not in detail and "pw@" not in detail
    assert "[redacted]" in detail
    assert "https://***@example.com/hook" in detail


def test_secret_hidden_by_control_characters_is_still_redacted(store):
    store.add_redaction("SECRETVALUE")
    run(store.audit.append("a", "x", "s", "SECRET\x00VALUE"))
    assert "SECRET" not in all_rows(store)[0]["detail"]


def test_empty_redaction_is_ignored(store):
    store.add_redaction("")
    run(store.audit.append("a", "x", "s", "keep me"))
    assert all_rows(store)[0]["detail"] == "keep me"


def test_direct_update_and_delete_fail_with_the_trigger_message(store):
    run(store.audit.append("a", "x", "s"))
    for sql in ("UPDATE audit_log SET actor = 'z'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.Error, match="append-only"):
            store.transact_sync(lambda tx, sql=sql: tx.execute(sql))
    assert len(all_rows(store)) == 1


def test_public_surface_is_append_append_tx_and_read_only(store):
    public = {n for n in dir(store.audit) if not n.startswith("_")}
    assert public == {"append", "append_tx", "read"}
    assert isinstance(store.audit, AuditLog)


def test_trimming_events_leaves_audit_rows(store):
    run(store.audit.append("a", "x", "s"))
    store.transact_sync(lambda tx: tx.execute(
        "INSERT INTO events (id, ts, source, host, app, severity, message, ingested) "
        "VALUES (1, 1, 'api', 'h', 'a', 3, 'm', 1)"))
    store.transact_sync(lambda tx: tx.execute("DELETE FROM events"))
    assert len(all_rows(store)) == 1


def test_append_tx_rolls_back_with_its_transaction(store):
    def fn(tx):
        store.audit.append_tx(tx, "a", "x", "s")
        raise KeyError("boom")

    with pytest.raises(KeyError):
        run(store.transact(fn))
    assert all_rows(store) == []


def test_append_tx_commits_with_its_transaction_and_returns_row_id(store):
    got = run(store.transact(lambda tx: store.audit.append_tx(tx, "a", "x", "s", proposal_id="p1")))
    assert all_rows(store)[0]["id"] == got
