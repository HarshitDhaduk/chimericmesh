import sqlite3

import pytest

from chimericmesh.store.errors import StoreError
from chimericmesh.store.migrations import apply_migrations
from chimericmesh.store.schema import core_registry
from chimericmesh.store.tx import SubscriberError, Tx, counter_name_part, format_id, run_callbacks

import re


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    apply_migrations(c, core_registry())
    yield c
    c.close()


@pytest.fixture
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    return Tx(conn)


def test_counter_next_empty_gives_one_and_stores(tx):
    assert tx.counter_next("proposal") == 1
    assert tx.counter_get("proposal") == 1
    assert tx.counter_next("proposal") == 2


@pytest.mark.parametrize("stored,floor,expected", [(5, 9, 10), (9, 5, 10), (0, 0, 1), (7, 7, 8)])
def test_counter_next_is_max_of_counter_and_floor_plus_one(conn, stored, floor, expected):
    conn.execute("INSERT INTO counters VALUES ('proposal', ?)", (stored,))
    conn.execute("BEGIN IMMEDIATE")
    tx = Tx(conn)
    assert tx.counter_next("proposal", floor=floor) == expected
    assert tx.counter_get("proposal") == expected


def test_counter_set_overwrites_even_downwards(tx):
    tx.counter_set("clean_shutdown", 1)
    tx.counter_set("clean_shutdown", 0)
    assert tx.counter_get("clean_shutdown") == 0
    with pytest.raises(ValueError):
        tx.counter_set("Bad Name", 1)


def test_counter_get_missing_is_zero(tx):
    assert tx.counter_get("nothing") == 0


def test_counter_raise_never_lowers(tx):
    tx.counter_raise("event_id", 10)
    tx.counter_raise("event_id", 4)
    assert tx.counter_get("event_id") == 10
    tx.counter_raise("event_id", 12)
    assert tx.counter_get("event_id") == 12


def test_counter_add_adds_delta_and_returns_value(tx):
    assert tx.counter_add("events_total", 3) == 3
    assert tx.counter_add("events_total", 4) == 7
    assert tx.counter_add("events_total", 0) == 7


@pytest.mark.parametrize("bad", ["", "Proposal", "1abc", "a b", "x'; DROP TABLE counters; --", "a" * 49, "é"])
def test_counter_names_are_validated(tx, bad):
    for call in (tx.counter_next, tx.counter_get):
        with pytest.raises(ValueError):
            call(bad)
    with pytest.raises(ValueError):
        tx.counter_add(bad, 1)
    with pytest.raises(ValueError):
        tx.counter_raise(bad, 1)


def test_valid_counter_names_accepted(tx):
    for name in ("proposal", "evsrc:syslog", "a" + "b" * 47, "rule", "evsrc:weird_source_"):
        tx.counter_add(name, 1)


def test_counter_name_part_makes_a_valid_name():
    part = counter_name_part("Weird Source!")
    assert part == "weird_source_"
    assert re.fullmatch(r"[a-z0-9_.-]{0,30}", part)
    assert len(counter_name_part("x" * 100)) == 30
    assert counter_name_part("") == ""
    assert counter_name_part("é\n\x00") == "___"


def test_format_id():
    assert format_id("p", 7) == "p7"
    assert format_id("r", 12) == "r12"


def test_execute_one_all_executemany(tx):
    tx.executemany(
        "INSERT INTO audit_log (ts, actor, action, subject) VALUES (?, ?, ?, ?)",
        [(1.0, "a", "x", "s"), (2.0, "b", "y", "t")],
    )
    row = tx.one("SELECT actor FROM audit_log WHERE id = ?", (2,))
    assert row["actor"] == "b"
    assert tx.one("SELECT actor FROM audit_log WHERE id = ?", (99,)) is None
    assert [r["actor"] for r in tx.all("SELECT actor FROM audit_log ORDER BY id")] == ["a", "b"]
    assert tx.execute("SELECT 1").fetchone()[0] == 1


def test_savepoint_rolls_back_only_its_scope(tx):
    tx.counter_add("keep", 1)
    with pytest.raises(RuntimeError):
        with tx.savepoint():
            tx.counter_add("lost", 1)
            raise RuntimeError("boom")
    with tx.savepoint():
        with tx.savepoint():
            tx.counter_add("nested", 1)
    assert tx.counter_get("keep") == 1 and tx.counter_get("lost") == 0 and tx.counter_get("nested") == 1


def test_run_callbacks_runs_in_order_with_same_tx(tx):
    seen = []

    def make(name):
        def cb(t, *args):
            seen.append((name, t is tx, args))

        return cb

    run_callbacks(tx, [("a", make("a")), ("b", make("b"))], 1, "x")
    assert seen == [("a", True, (1, "x")), ("b", True, (1, "x"))]


def test_raising_callback_raises_subscriber_error_naming_it_and_rolls_back_all(conn):
    conn.execute("BEGIN IMMEDIATE")
    tx = Tx(conn)

    def good(t):
        t.counter_add("written_by_good", 1)

    def bad(t):
        t.counter_add("written_by_bad", 1)
        raise ValueError("nope")

    with pytest.raises(SubscriberError) as ei:
        run_callbacks(tx, [("good", good), ("bad", bad), ("never", good)])
    assert ei.value.name == "bad" and isinstance(ei.value.cause, ValueError)
    assert "bad" in str(ei.value)
    assert isinstance(ei.value, StoreError)
    conn.execute("ROLLBACK")  # what transact does when fn raises
    assert conn.execute("SELECT COUNT(*) FROM counters").fetchone()[0] == 0
