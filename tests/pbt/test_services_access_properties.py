"""Properties 13 and 15: the Host allow-list and the reduce limiter accounting."""
from __future__ import annotations
import asyncio

from hypothesis import given, settings, strategies as st

from chimericmesh.services.access import host_allowed
from chimericmesh.services.limiter import REDUCE_OPS_PER_MINUTE, ReduceAuditLimiter

ALLOWED = frozenset({"127.0.0.1", "localhost", "::1", "dev.example"})
FORBIDDEN = set("@ \\") | {chr(c) for c in list(range(0x20)) + list(range(0x7F, 0xA0))}


def _case(text, flips):
    return "".join(c.upper() if f else c.lower() for c, f in zip(text, flips + [False] * len(text)))


# Feature: services, Property 13: Host allow-list
@given(host=st.sampled_from(sorted(ALLOWED)), flips=st.lists(st.booleans(), max_size=20),
       port=st.one_of(st.none(), st.integers(0, 65535)))
def test_allowed_host_any_case_any_port(host, flips, port):
    shown = f"[{host}]" if ":" in host else _case(host, flips)
    header = shown if port is None else f"{shown}:{port}"
    assert host_allowed(header, ALLOWED) is True


@given(header=st.text(max_size=400))
def test_true_only_for_wellformed_allowed_hosts(header):
    if not host_allowed(header, ALLOWED):
        return
    assert len(header) <= 300 and not (set(header) & FORBIDDEN)
    host = header
    if header.startswith("["):
        host, _, rest = header.partition("]")
        host = host[1:]
        assert rest == "" or (rest.startswith(":") and rest[1:].isdigit() and rest.count(":") == 1)
    elif ":" in header:
        host, _, port = header.partition(":")
        assert port.isdigit() and ":" not in port
    assert host.casefold() in ALLOWED


@given(host=st.sampled_from(sorted(ALLOWED)), bad=st.sampled_from(["@", " ", "\\", "\x00", "\n", "\x1f", "\x85"]),
       pos=st.integers(0, 12))
def test_forbidden_characters_reject(host, bad, pos):
    pos = min(pos, len(host))
    assert host_allowed(host[:pos] + bad + host[pos:], ALLOWED) is False


@given(extra=st.integers(0, 400))
def test_overlong_header_rejected(extra):
    assert host_allowed("a" * (301 + extra), frozenset({"a" * (301 + extra)})) is False


@given(host=st.sampled_from(["127.0.0.1", "localhost"]), port=st.integers(0, 65535))
def test_second_colon_outside_brackets_rejected(host, port):
    assert host_allowed(f"{host}:{port}:{port}", ALLOWED) is False


# Feature: services, Property 15: Reduce limiter accounting
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Audit:
    def __init__(self):
        self.own = 0
        self.suppressed = 0

    async def append(self, actor, action, subject, detail="", **kw):
        if action == "reduce_ops_suppressed":
            self.suppressed += int(detail.split("=")[1])
        else:
            self.own += 1


def _model(calls):
    """Expected True/False per call: windows of 60 s per key, first 10 calls each."""
    state: dict[str, list[float]] = {}
    out = []
    for t, key in calls:
        start, used = state.get(key, (None, 0))
        if start is None or t - start >= 60:
            start, used = t, 0
        out.append(used < REDUCE_OPS_PER_MINUTE)
        state[key] = (start, used + 1)
    return out


@settings(deadline=None, max_examples=60)
@given(steps=st.lists(st.tuples(st.integers(0, 90), st.sampled_from(["a", "b", "c", "d", "e"])), max_size=120))
def test_limiter_accounting(steps):
    clock, audit = _Clock(), _Audit()
    calls = []
    t = 0.0
    for dt, key in steps:
        t += dt
        calls.append((t, key))

    async def go():
        lim = ReduceAuditLimiter(audit, clock)
        got = []
        for when, key in calls:
            clock.t = when
            got.append(await lim.record(key, actor="x", action="rule_paused", subject=key))
            if when % 7 < 3:
                await lim.flush_due()
        await lim.aclose()
        return got

    got = asyncio.run(go())
    assert len(got) == len(calls)                           # every call returned, so every action ran
    assert got == _model(calls)
    assert audit.own == sum(got)
    assert audit.own + audit.suppressed == len(calls)
