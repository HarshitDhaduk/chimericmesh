"""Property 17 of auto-approval: rule ids are unique across restarts."""
from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_rules import make_rule  # noqa: E402

from chimericmesh.approvals.rulebook import insert_rule, issue_rule_id  # noqa: E402
from chimericmesh.approvals.schema import approvals_registry  # noqa: E402
from chimericmesh.store import Store  # noqa: E402

ACTIONS = st.lists(st.sampled_from(["create", "create", "revoke", "delete_last", "restart"]), max_size=14)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ACTIONS)
def test_property_17_ids_are_never_reused(actions):
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "p.db")
        store = Store.open(path, registry=approvals_registry())
        seen: list[str] = []

        def create():
            def write(tx):
                seq, rid = issue_rule_id(tx)
                insert_rule(tx, dataclasses.replace(make_rule(), id=rid, seq=seq))
                return rid
            return store.transact_sync(write, allow_blocking=True)

        for action in actions:
            if action == "create":
                seen.append(create())
            elif action == "revoke" and seen:
                store.transact_sync(lambda tx: tx.execute("UPDATE approval_rules SET status = 'revoked' "
                                                          "WHERE id = ?", (seen[-1],)), allow_blocking=True)
            elif action == "delete_last" and seen:
                store.transact_sync(lambda tx: tx.execute("DELETE FROM approval_rules WHERE id = ?", (seen[-1],)),
                                    allow_blocking=True)
            elif action == "restart":
                store.close()
                store = Store.open(path, registry=approvals_registry())
        store.close()
        assert len(seen) == len(set(seen))
        assert seen == [f"r{n}" for n in range(1, len(seen) + 1)]
