"""Properties 7 and 8: tagging precedence, determinism and totality."""
from __future__ import annotations

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.services.tagging import build_index, resolve_service
from chimericmesh.services.models import LogSource, Service


def svc(sid, seq, *, hosts=(), apps=(), sources=()):
    ls = tuple(
        LogSource(id=s, service_id=sid, path="/x", format="plain", enabled=True, backfill_lines=0,
                  origin="user", host=None, created_at=0.0, updated_at=0.0)
        for s in sources
    )
    return Service(id=sid, name=sid, hosts=tuple(hosts), app_names=tuple(apps), protocols=(),
                   log_sources=ls, code_folder=None, created_at=0.0, updated_at=0.0, seq=seq)

names = st.sampled_from(["a", "B", "c", "D", "web", "WEB", "x1"])
hostnames = st.sampled_from(["h1", "H2", "h3", "box", "BOX"])


@st.composite
def service_sets(draw):
    n = draw(st.integers(0, 6))
    out = []
    used = 0
    for i in range(n):
        srcs = [f"ls{used + k}" for k in range(draw(st.integers(0, 2)))]
        used += len(srcs)
        out.append(svc(f"s{i}", i + 1, hosts=draw(st.lists(hostnames, max_size=3)),
                       apps=draw(st.lists(names, max_size=3)), sources=srcs))
    return out


def model(services, host, app, lsid):
    ordered = sorted(services, key=lambda s: s.seq)
    if lsid is not None:
        for s in ordered:
            if any(x.id == lsid for x in s.log_sources):
                return s.id
    for s in ordered:
        if app.casefold() in {a.casefold() for a in s.app_names}:
            return s.id
    for s in ordered:
        if host.casefold() in {h.casefold() for h in s.hosts}:
            return s.id
    return None


@settings(max_examples=300, deadline=None)
@given(service_sets(), hostnames, names, st.one_of(st.none(), st.sampled_from(["ls0", "ls1", "ls2", "ls9"])),
       st.randoms(use_true_random=False))
def test_property_7_precedence_and_determinism(services, host, app, lsid, rnd):
    expected = model(services, host, app, lsid)
    shuffled = list(services)
    rnd.shuffle(shuffled)
    for order in (services, shuffled, list(reversed(services))):
        assert resolve_service(build_index(order), "syslog", host, app, lsid) == expected


@settings(max_examples=300, deadline=None)
@given(service_sets(), st.text(max_size=200), st.text(max_size=200), st.text(max_size=200),
       st.one_of(st.none(), st.text(max_size=50)))
def test_property_8_total_and_closed(services, source, host, app, lsid):
    idx = build_index(services)
    got = resolve_service(idx, source, host, app, lsid)
    assert got is None or got in {s.id for s in services}
