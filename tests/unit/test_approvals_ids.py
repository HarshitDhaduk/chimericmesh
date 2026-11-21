"""Rule ids across restarts, at the rulebook level (the manager-level test is in test_approvals_rules)."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import dataclasses
from pathlib import Path

from approvals_rules import make_rule

from chimericmesh.approvals.rulebook import insert_rule, issue_rule_id, load_rules
from chimericmesh.approvals.schema import approvals_registry
from chimericmesh.store import Store


def open_store(path: Path) -> Store:
    return Store.open(str(path), registry=approvals_registry())


def create(store: Store) -> str:
    def write(tx):
        seq, rid = issue_rule_id(tx)
        insert_rule(tx, dataclasses.replace(make_rule(), id=rid, seq=seq))
        return rid

    return store.transact_sync(write, allow_blocking=True)


def test_ids_continue_after_a_restart_and_are_never_reused(tmp_path: Path):
    path = tmp_path / "ids.db"
    s = open_store(path)
    assert [create(s), create(s)] == ["r1", "r2"]
    s.close()
    s = open_store(path)
    assert create(s) == "r3"
    s.transact_sync(lambda tx: tx.execute("DELETE FROM approval_rules WHERE id = 'r3'"), allow_blocking=True)
    s.close()
    s = open_store(path)
    assert create(s) == "r4"
    s.close()


def test_a_lowered_counter_never_gives_an_id_below_a_stored_one(tmp_path: Path):
    s = open_store(tmp_path / "low.db")
    for _ in range(3):
        create(s)
    s.transact_sync(lambda tx: tx.counter_set("rule", 0), allow_blocking=True)
    assert create(s) == "r4"
    s.close()


def test_concurrent_creates_get_distinct_ids(tmp_path: Path):
    s = open_store(tmp_path / "conc.db")

    async def run():
        def write(tx):
            seq, rid = issue_rule_id(tx)
            insert_rule(tx, dataclasses.replace(make_rule(), id=rid, seq=seq))
            return rid

        return await asyncio.gather(*(s.transact(write) for _ in range(8)))

    ids = asyncio.run(run())
    s.close()
    assert len(set(ids)) == 8


def test_counter_and_insert_commit_together(tmp_path: Path):
    s = open_store(tmp_path / "atomic.db")
    create(s)

    def failing(tx):
        seq, rid = issue_rule_id(tx)
        insert_rule(tx, dataclasses.replace(make_rule(), id=rid, seq=seq))
        raise RuntimeError("boom")

    try:
        s.transact_sync(failing, allow_blocking=True)
    except RuntimeError:
        pass
    assert s.read_sync(lambda tx: tx.counter_get("rule"), allow_blocking=True) == 1
    assert [r.id for r in s.read_sync(load_rules, allow_blocking=True)] == ["r1"]
    s.close()
