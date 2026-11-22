"""Properties 7, 8 and 9 of agent-loop: the work set, reopen, and the long-poll bounds."""
from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path
from hypothesis import given, settings
from hypothesis import strategies as st
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from agent_helpers import FakeState, make_incident, make_proposal  # noqa: E402
from chimericmesh.agent.errors import TooManyWaiters  # noqa: E402
from chimericmesh.agent.ports import AgentPorts  # noqa: E402
from chimericmesh.agent.watch import WatchQueue, evaluate_work  # noqa: E402
REGISTERED = {"a", "b"}
SERVICE_IDS = ["a", "b", "gone", None]
def registered(sid: str) -> bool:
    return sid in REGISTERED
def approval_of(prop):
    return {"by": "user", "rule_id": None, "rule_status_now": None, "approved_at": 1.0, "expired": False}
@st.composite
def worlds(draw):
    n_inc = draw(st.integers(0, 14))
    incidents = [make_incident(f"i{n}", service_id=draw(st.sampled_from(SERVICE_IDS)), severity=draw(st.integers(0, 7)),
                               count=draw(st.integers(1, 6)), opening=draw(st.integers(1, 3)),
                               resolved=draw(st.booleans())) for n in range(n_inc)]
    proposals = []
    for n in range(draw(st.integers(0, 24))):
        proposals.append(make_proposal(
            f"p{n}", f"i{draw(st.integers(0, max(n_inc, 1)))}", status=draw(st.sampled_from(
                ["pending", "approved", "denied", "done"])),
            service_id=draw(st.sampled_from(SERVICE_IDS)), decision_origin=draw(st.sampled_from([None, "user", "hub"])),
            incident_opening=draw(st.sampled_from([None, 1, 2, 3])), created=float(n),
            decided_by=draw(st.sampled_from(["", "alice", "expired-bob", "rule:r1"]))))
    exclude = frozenset(draw(st.lists(st.sampled_from([f"i{n}" for n in range(15)] + [f"p{n}" for n in range(25)]),
                                      max_size=8)))
    wanted = draw(st.sampled_from([None, "a", "b", "gone", "unassigned"]))
    return incidents, proposals, exclude, wanted
def oracle_incident(inc, proposals, exclude, wanted) -> bool:
    if inc.resolved or inc.id in exclude:
        return False
    for p in proposals:
        if p.incident_id != inc.id:
            continue
        if p.status in ("pending", "approved"):
            return False
        if p.status == "denied" and p.decision_origin == "user" and p.incident_opening == inc.opening:
            return False
    if inc.service_id is None:
        return wanted == "unassigned"
    if wanted == "unassigned":
        return False
    if wanted is not None and inc.service_id != wanted:
        return False
    return inc.service_id in REGISTERED
def oracle_proposal(p, exclude, wanted) -> bool:
    if p.status != "approved" or p.id in exclude:
        return False
    if p.service_id is None:
        return wanted == "unassigned"
    if wanted == "unassigned" or (wanted is not None and p.service_id != wanted):
        return False
    return p.service_id in REGISTERED
@settings(max_examples=100, deadline=None)
@given(worlds())
def test_property_7_work_set_is_exactly_the_specified_set(world):
    incidents, proposals, exclude, wanted = world
    out = evaluate_work(incidents, proposals, service_id=wanted, exclude=exclude, registered=registered,
                        approval_of=approval_of)
    expected = sorted((i for i in incidents if oracle_incident(i, proposals, exclude, wanted)),
                      key=lambda i: (i.severity, -i.count, i.id))
    assert [i["id"] for i in out.incidents] == [i.id for i in expected[:5]]
    assert len(out.incidents) <= 5
    keys = [(i["severity"], -i["count"], i["id"]) for i in out.incidents]
    assert keys == sorted(keys)
    exp_props = sorted((p for p in proposals if oracle_proposal(p, exclude, wanted)), key=lambda p: (p.created, p.id))
    assert [p["id"] for p in out.approved_proposals] == [p.id for p in exp_props[:5]]
    if wanted != "unassigned":
        assert all(i["service_id"] is not None for i in out.incidents)
        assert all(p["service_id"] is not None for p in out.approved_proposals)
@settings(max_examples=60, deadline=None)
@given(st.integers(1, 3), st.sampled_from(["user", "hub"]))
def test_property_7_user_denial_hides_and_hub_denial_does_not(opening, origin):
    p = make_proposal("p1", "i1", status="denied", decision_origin=origin, incident_opening=opening)
    out = evaluate_work([make_incident("i1", opening=opening)], [p], service_id=None, exclude=frozenset(),
                        registered=lambda s: True, approval_of=approval_of)
    assert (len(out.incidents) == 0) is (origin == "user")
@settings(max_examples=60, deadline=None)
@given(st.integers(1, 4), st.integers(1, 4))
def test_property_8_reopen_returns_a_denied_incident(denied_opening, opening):
    denial = make_proposal("p1", "i1", status="denied", decision_origin="user", incident_opening=denied_opening)
    out = evaluate_work([make_incident("i1", opening=opening)], [denial], service_id=None, exclude=frozenset(),
                        registered=lambda s: True, approval_of=approval_of)
    hidden = opening == denied_opening
    assert (not out.incidents) is hidden
    if opening > denied_opening:
        assert [i["id"] for i in out.incidents] == ["i1"]
class Registry:
    def get(self, sid):
        return object()
@settings(max_examples=12, deadline=None)
@given(st.sampled_from([0.05, 0.1, 0.2, 0.3]), st.integers(1, 7),
       st.lists(st.sampled_from(["cancel", "return", "close"]), min_size=1, max_size=3))
def test_property_9_long_poll_bounds(timeout, n_waiters, endings):
    tick = 0.02
    q = WatchQueue(AgentPorts(state=FakeState(), registry=Registry(), approvals=None, protected_dirs=(),  # type: ignore[arg-type]
                              workspace_root=None), tick=tick)
    async def go():
        tasks = [asyncio.create_task(q.next(timeout=timeout, service_id=None, exclude=frozenset()))
                 for _ in range(n_waiters)]
        start = time.monotonic()
        await asyncio.sleep(0)
        for t in tasks:
            if t.done():
                assert isinstance(t.exception(), TooManyWaiters)
        assert q.waiting <= 4
        ending = endings[0]
        if ending == "cancel":
            for t in tasks:
                t.cancel()
        elif ending == "close":
            q.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        took = time.monotonic() - start
        assert took <= timeout + tick + 0.15
        refused = [r for r in results if isinstance(r, TooManyWaiters)]
        assert len(refused) == max(0, n_waiters - 4)
        assert q.waiting == 0
    asyncio.run(go())
