"""Property 18 of auto-approval (optional): persisted rules round trip."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_rules import make_rule  # noqa: E402

from chimericmesh.approvals.rulebook import insert_rule, load_rules  # noqa: E402
from chimericmesh.approvals.schema import approvals_registry  # noqa: E402
from chimericmesh.store import Store  # noqa: E402

rules = st.builds(
    make_rule,
    match_type=st.sampled_from(["signature", "category"]),
    match_value=st.text(min_size=1, max_size=200).filter(lambda s: "\x00" not in s and s == s.encode("utf-8", "replace").decode()),
    most_urgent_severity=st.sampled_from([3, 4]),
    expires_at=st.floats(1, 2e9, allow_nan=False),
    max_per_hour=st.integers(1, 10), max_per_incident=st.integers(1, 3),
    max_files=st.integers(1, 20), max_lines=st.integers(1, 500),
    status=st.sampled_from(["active", "suspended", "revoked", "expired"]),
    note=st.text(max_size=200).filter(lambda s: s == s.encode("utf-8", "replace").decode()),
    use_count=st.integers(0, 1000),
    source_proposal_id=st.one_of(st.none(), st.just("p1")),
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rules)
def test_property_18_rule_survives_a_write_and_a_read(rule):
    store = Store.open(":memory:", registry=approvals_registry())
    try:
        store.transact_sync(lambda tx: insert_rule(tx, dataclasses.replace(rule, id="r1", seq=1)), allow_blocking=True)
        (back,) = store.read_sync(load_rules, allow_blocking=True)
        assert back == dataclasses.replace(rule, id="r1", seq=1)
    finally:
        store.close()
