"""Properties 14, 16, 20 and 21 (the NICK sequence part of 20 belongs to the IRC wiring, task 7.3)."""
from __future__ import annotations
import asyncio
import logging

from hypothesis import given, settings, strategies as st

from chimericmesh.services.access import (
    AccessControl,
    AdminResult,
    OperatorVerdict,
    is_reserved_nick,
    operator_verdict,
)
from chimericmesh.services.token import TokenStore

HOSTS = frozenset({"localhost"})


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Audit:
    def __init__(self):
        self.rows = []

    async def append(self, actor, action, subject, detail="", **kw):
        self.rows.append((actor, action, subject, detail))


def _ac(clock=None, audit=None):
    tokens = TokenStore.open(None, in_memory=True)
    return AccessControl(enforce=True, tokens=tokens, allowed_hosts=HOSTS, audit=audit,
                         now=clock or _Clock())


# Feature: services, Property 14: Token comparison
@given(x=st.one_of(st.none(), st.text(max_size=300)))
def test_verify_token_iff_equal_and_never_raises(x):
    ac = _ac()
    assert ac.verify_token(x) is (x == ac.tokens.current())
    assert ac.verify_pass(x) is (x == ac.tokens.current())


@given(n=st.integers(0, 43))
def test_every_proper_prefix_and_extension_fails(n):
    ac = _ac()
    tok = ac.tokens.current()
    assert ac.verify_token(tok[:n]) is (tok[:n] == tok)
    assert ac.verify_token(tok + "x") is False


# Feature: services, Property 16: Wrong-token limiter
ops = st.lists(st.tuples(st.sampled_from(["a", "b", "c"]), st.booleans(), st.integers(0, 40)), max_size=80)


async def _run(ops_list):
    clock = _Clock()
    ac = _ac(clock, _Audit())
    out = []
    for addr, right, dt in ops_list:
        clock.t += dt
        res = await ac.check_admin(ac.tokens.current() if right else "wrong", client=addr, operation="op")
        out.append((addr, right, clock.t, res.result))
    return out


@settings(max_examples=60, deadline=None)
@given(ops_list=ops)
def test_wrong_token_limiter(ops_list):
    out = asyncio.run(_run(ops_list))
    for addr in "abc":
        mine = [(r, t, res) for a, r, t, res in out if a == addr]
        for right, t, res in mine:
            if right:
                assert res is AdminResult.OK                       # never 401 or 429
        unauth = [t for r, t, res in mine if res is AdminResult.UNAUTHORIZED]
        for i, t in enumerate(unauth):
            assert len([u for u in unauth if t - 60 < u <= t]) <= 10
        # another address does not change this address's outcomes: replay only this address at the same times
        replay = _replay_absolute(addr, out)
        assert [res for *_, res in replay] == [res for a, _, _, res in out if a == addr]


def _replay_absolute(addr, out):
    async def go():
        clock = _Clock()
        ac = _ac(clock, _Audit())
        res = []
        for a, right, t, _ in out:
            if a != addr:
                continue
            clock.t = t
            outcome = await ac.check_admin(ac.tokens.current() if right else "wrong", client=a, operation="op")
            res.append((a, right, t, outcome.result))
        return res
    return asyncio.run(go())


# Feature: services, Property 20: IRC verdict
@given(nick=st.text(max_size=20), operators=st.one_of(st.none(), st.sets(st.text(max_size=20), max_size=4)),
       pass_ok=st.booleans())
def test_verdict_allowed_iff(nick, operators, pass_ok):
    verdict = operator_verdict(nick=nick, operators=operators, pass_ok=pass_ok)
    expected = bool(operators) and nick.lower() in {o.lower() for o in operators} and pass_ok
    assert (verdict is OperatorVerdict.ALLOWED) is bool(expected)
    assert verdict is not OperatorVerdict.BASELINE


@given(nick=st.text(max_size=20), flips=st.lists(st.booleans(), max_size=12))
def test_reserved_nick_any_case(nick, flips):
    assert is_reserved_nick(nick) is (nick.strip().casefold() in {"dashboard", "chimericbot"})
    for base in ("dashboard", "chimericbot"):
        cased = "".join(c.upper() if f else c for c, f in zip(base, flips + [False] * len(base)))
        assert is_reserved_nick(cased) and is_reserved_nick(f" {cased} ")


# Feature: services, Property 21: No token material in outputs
@settings(max_examples=40, deadline=None)
@given(presented=st.text(max_size=60), name=st.text(max_size=40), n=st.integers(1, 15))
def test_no_token_material_in_audit_or_logs(presented, name, n):
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = Grab(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    audit = _Audit()
    ac = _ac(audit=audit)
    real = ac.tokens.current()
    canary = "canary-" + presented + name

    async def go():
        for i in range(n):
            await ac.check_admin(canary, client="1.1.1.1", operation=f"POST /api/{i}")
        await ac.check_admin(real, client="1.1.1.1", operation="POST /api/ok")
    try:
        asyncio.run(go())
    finally:
        root.removeHandler(handler)
    text = repr(audit.rows) + " ".join(records) + repr(ac) + repr(ac.tokens)
    assert real not in text
    assert canary not in repr(audit.rows) + " ".join(records)
