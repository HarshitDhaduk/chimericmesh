"""IRC policy functions (R9.1 to R9.5)."""
from __future__ import annotations
import itertools

import pytest

from chimericmesh.services.access import (
    RESERVED_NICKS,
    AccessControl,
    OperatorVerdict,
    is_reserved_nick,
    operator_verdict,
)
from chimericmesh.services.token import TokenStore


@pytest.mark.parametrize("nick", ["dashboard", "DASHBOARD", "ChimericBot", "chimericbot", " Dashboard"])
def test_reserved(nick):
    assert is_reserved_nick(nick) is True


@pytest.mark.parametrize("nick", ["dashboard2", "bot", "alice", "", "dash board"])
def test_not_reserved(nick):
    assert is_reserved_nick(nick) is False


def test_verdict_truth_table():
    V = OperatorVerdict
    for nick, pass_ok in itertools.product(["alice", "bob", ""], [True, False]):
        assert operator_verdict(nick=nick, operators=None, pass_ok=pass_ok) is V.OPERATORS_UNSET
        assert operator_verdict(nick=nick, operators=set(), pass_ok=pass_ok) is V.OPERATORS_UNSET
    assert operator_verdict(nick="bob", operators={"alice"}, pass_ok=True) is V.NOT_OPERATOR
    assert operator_verdict(nick="ALICE", operators={"alice"}, pass_ok=False) is V.NEEDS_PASS
    assert operator_verdict(nick="Alice", operators={"alice"}, pass_ok=True) is V.ALLOWED
    assert operator_verdict(nick="alice", operators={"Alice"}, pass_ok=True) is V.ALLOWED


def test_baseline_exists_and_is_never_returned():
    assert OperatorVerdict.BASELINE is not None
    seen = {operator_verdict(nick=n, operators=o, pass_ok=p)
            for n in ("alice", "x") for o in (None, set(), {"alice"}) for p in (True, False)}
    assert OperatorVerdict.BASELINE not in seen


def test_access_control_verify_pass():
    tokens = TokenStore.open(None, in_memory=True)
    ac = AccessControl(enforce=True, tokens=tokens, allowed_hosts=frozenset({"localhost"}), audit=None)
    assert ac.verify_pass(tokens.current()) is True
    assert ac.verify_pass("wrong") is False
    assert ac.verify_pass("x" * 10_000) is False
    assert ac.operator_verdict("alice", operators={"alice"}, pass_ok=True) is OperatorVerdict.ALLOWED


def test_reserved_nicks_match_hub_constants():
    from chimericmesh.hub import BOT_NICK, WEB_NICK
    assert RESERVED_NICKS == frozenset({WEB_NICK.lower(), BOT_NICK.lower()})
