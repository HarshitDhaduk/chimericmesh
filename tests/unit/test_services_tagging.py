"""Service index and resolve_service (R4.1 to R4.3, R4.7), one test per sub-criterion 4.2a to 4.2e."""
from __future__ import annotations

import os
import time

import pytest

from chimericmesh.services.models import LogSource, Service
from chimericmesh.services.tagging import EMPTY_INDEX, build_index, resolve_service


def svc(sid, seq, *, hosts=(), apps=(), sources=()):
    ls = tuple(
        LogSource(id=s, service_id=sid, path="/x", format="plain", enabled=True, backfill_lines=0,
                  origin="user", host=None, created_at=0.0, updated_at=0.0)
        for s in sources
    )
    return Service(id=sid, name=sid, hosts=tuple(hosts), app_names=tuple(apps), protocols=(),
                   log_sources=ls, code_folder=None, created_at=0.0, updated_at=0.0, seq=seq)


def test_4_2a_without_log_source_app_then_host_decide():
    idx = build_index([svc("a", 1, hosts=["h1"]), svc("b", 2, apps=["nginx"])])
    assert resolve_service(idx, "syslog", "h1", "nginx", None) == "b"
    assert resolve_service(idx, "syslog", "h1", "other", None) == "a"


def test_4_2b_known_log_source_wins_over_app_and_host():
    idx = build_index([svc("a", 1, sources=["ls1"]), svc("b", 2, hosts=["h"], apps=["x"])])
    assert resolve_service(idx, "api", "h", "x", "ls1") == "a"


def test_4_2c_app_name_case_insensitive():
    idx = build_index([svc("a", 1, apps=["NginX"])])
    assert resolve_service(idx, "s", "h", "nGINx", None) == "a"


def test_4_2d_host_case_insensitive():
    idx = build_index([svc("a", 1, hosts=["Web-01"])])
    assert resolve_service(idx, "s", "WEB-01", "z", None) == "a"


def test_4_2e_no_match_is_none():
    idx = build_index([svc("a", 1, hosts=["h"], apps=["x"])])
    assert resolve_service(idx, "s", "other", "other", None) is None
    assert resolve_service(idx, "s", "other", "other", "ls9") is None


def test_unknown_log_source_falls_through_to_app_and_host():
    idx = build_index([svc("a", 1, apps=["x"])])
    assert resolve_service(idx, "s", "h", "x", "ls404") == "a"


def test_first_created_wins_regardless_of_build_order():
    a, b = svc("a", 1, apps=["x"], hosts=["h"]), svc("b", 2, apps=["x"], hosts=["h"])
    for order in ([a, b], [b, a]):
        idx = build_index(order)
        assert resolve_service(idx, "s", "h", "x", None) == "a"
        assert resolve_service(idx, "s", "h", "q", None) == "a"


def test_empty_index_resolves_to_none():
    assert resolve_service(build_index([]), "s", "h", "a", "ls1") is None
    assert resolve_service(EMPTY_INDEX, "s", "h", "a", None) is None


def test_returns_only_ids_in_the_index():
    idx = build_index([svc("a", 1, apps=["x"])])
    assert resolve_service(idx, "s", "h", "x", None) in {"a", None}


def test_huge_and_hostile_inputs_do_not_raise():
    idx = build_index([svc("a", 1, apps=["x"])])
    big = "A\x00" * 500_000
    assert resolve_service(idx, big, big, big, big) is None
    assert resolve_service(idx, "", "", "", "") is None


@pytest.mark.skipif(os.environ.get("CHIMERICMESH_SKIP_BENCH") == "1", reason="benchmark")
def test_lookup_under_one_millisecond():
    services = [svc(f"s{i}", i, hosts=[f"h{i}-{j}" for j in range(20)], apps=[f"a{i}-{j}" for j in range(20)])
                for i in range(100)]
    idx = build_index(services)
    n = 20_000
    start = time.perf_counter()
    for i in range(n):
        resolve_service(idx, "syslog", f"h{i % 100}-{i % 20}", "none", None)
    assert (time.perf_counter() - start) / n < 0.001
