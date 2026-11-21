"""Properties 8 and 9 of auto-approval: per-opening counting and the cooldown formula."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.approvals.ledger import UsageLedger

SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
H = 3600.0

ops = st.lists(st.one_of(
    st.tuples(st.just("reserve"), st.sampled_from(["r1", "r2"]), st.sampled_from(["i1", "i2"]), st.integers(1, 3),
              st.floats(0, 10 * H)),
    st.tuples(st.just("release"), st.integers(0, 30)),
), max_size=40)


@SETTINGS
@given(ops, st.floats(0, 12 * H))
def test_property_8_per_opening_counting(sequence, query_now):
    ledger = UsageLedger()
    live: dict[int, tuple[str, str, int, float]] = {}
    tokens: list[int] = []
    for op in sequence:
        if op[0] == "reserve":
            _, rule, inc, opening, at = op
            token = ledger.reserve(rule, inc, opening, at)
            tokens.append(token)
            live[token] = (rule, inc, opening, at)
        elif tokens:
            token = tokens[op[1] % len(tokens)]
            ledger.release(token)
            live.pop(token, None)
    for rule in ("r1", "r2"):
        for inc in ("i1", "i2"):
            for opening in (1, 2, 3):
                usage = ledger.usage(rule, inc, opening, query_now)
                assert usage.incident_opening == sum(
                    1 for r, i, o, _ in live.values() if (r, i, o) == (rule, inc, opening))
                assert usage.hour == sum(1 for r, _, _, at in live.values() if r == rule and query_now - at < H)


completions = st.lists(st.tuples(st.integers(1, 3), st.floats(0, 100 * H)), max_size=4)


@SETTINGS
@given(completions, st.floats(0, 120 * H), st.floats(1, 200 * H), st.floats(0, 200 * H))
def test_property_9_cooldown_formula_and_query_time_independence(done, reopen_seen, cooldown, later):
    ledger = UsageLedger()
    uses = {}
    for n, (opening, at) in enumerate(done):
        pid = f"p{n}"
        token = ledger.reserve("r1", "i1", opening, max(0.0, at - 10))
        ledger.bind(token, pid)
        ledger.completed(pid, at)
        uses[pid] = (opening, at)
    target = 4
    ledger.note_opening("i1", target, reopen_seen)
    ledger.note_opening("i1", target, reopen_seen + later + 1)  # a later sighting never moves the first
    answer = ledger.cooldown_for("i1", target, cooldown)
    qualifying = {pid: at for pid, (opening, at) in uses.items() if opening < target and reopen_seen - at < cooldown}
    if not qualifying:
        assert answer is None
    else:
        assert answer in qualifying and qualifying[answer] == max(qualifying.values())
    # the answer does not depend on when the question is asked
    assert ledger.cooldown_for("i1", target, cooldown) == answer
