"""Property 13: accept is all or nothing. Validates: Requirements 5.2, 5.5."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.discovery.accept import AcceptBody, Acceptor, RegistrySink
from chimericmesh.discovery.models import CandidatePath, Evidence, Scan, Suggestion
from chimericmesh.discovery.schema import discovery_registry
from chimericmesh.hub import Hub
from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})


class Mgr:
    os_name = "windows" if os.name == "nt" else "linux"

    def __init__(self):
        sug = Suggestion("sg1", "nginx", "n", "x", "high", Evidence("nginx", 1, 1, ()),
                         (CandidatePath("/x", "plain", "other", False, False, None, None),), "f" * 16)
        self.scan = Scan("sc1", "done", 1.0, 2.0, False, self.os_name, "", (), (sug,))

    def find(self, sid):
        return (self.scan, self.scan.suggestions[0]) if sid == "sg1" else None


def counts(store):
    q = lambda sql: store.read_sync(lambda tx: tx.one(sql)[0], allow_blocking=True)  # noqa: E731
    return (q("SELECT COUNT(*) FROM services"), q("SELECT COUNT(*) FROM log_sources"),
            q("SELECT COUNT(*) FROM service_tombstones"),
            q("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'service_%' OR action LIKE 'log_source_%'"))


INVALID = st.sampled_from(["relative.log", "", "x\x00y.log", "DIR", "NOFORMAT", "DUP"])


@settings(max_examples=40, deadline=None)
@given(st.integers(0, 6), st.integers(0, 6), INVALID, st.sampled_from(["path", "folder", "slug", "cap"]))
def test_invalid_entry_at_any_position_creates_nothing_and_valid_creates_exactly_the_paths(n_before, n_after, bad, kind):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        store = Store.open(":memory:", registry=discovery_registry())
        try:
            home = tmp / "home"
            home.mkdir()
            reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[str(home / ".c")], home=str(home))
            reg.set_known_formats(FORMATS)
            acc = Acceptor(Mgr(), RegistrySink(reg), reg, store, FORMATS)

            def mk(n, prefix):
                out = []
                for i in range(n):
                    p = tmp / f"{prefix}{i}.log"
                    p.write_text("x", encoding="utf-8")
                    out.append((str(p), "plain"))
                return out

            before, after = mk(n_before, "b"), mk(n_after, "a")
            valid = tuple(before + after) or tuple(mk(1, "only"))
            before_state = counts(store)
            if kind == "path":
                entry = {"DIR": (str(tmp), "plain"), "NOFORMAT": (str(tmp / "z.log"), "nope"),
                         "DUP": (valid[0][0], "plain")}.get(bad, (bad, "plain"))
                body = AcceptBody("svc", "S", tuple(before) + (entry,) + tuple(after) if (before or after) else (entry,))
                if bad == "DUP" and not (before or after):
                    body = AcceptBody("svc", "S", (valid[0], entry))
            elif kind == "folder":
                body = AcceptBody("svc", "S", valid, code_folder=str(home))
            elif kind == "slug":
                body = AcceptBody("Bad Slug", "S", valid)
            else:
                body = AcceptBody("svc", "S", tuple(valid) * 21 if len(valid) * 21 > 20 else valid)
            with pytest.raises(ValidationFailed):
                asyncio.run(acc.accept("sg1", body, actor="x"))
            assert counts(store) == before_state and reg.list() == []
            # the same suggestion can be accepted afterwards with a fully valid request, in the requested order
            svc, sources = asyncio.run(acc.accept("sg1", AcceptBody("svc", "S", valid), actor="x"))
            assert [os.path.normcase(s.path) for s in sources] == [os.path.normcase(os.path.normpath(p)) for p, _ in valid]
            assert all(s.origin == "discovery" for s in sources)
            assert [s.path for s in reg.get("svc").log_sources] == [s.path for s in sources]
        finally:
            store.close()
