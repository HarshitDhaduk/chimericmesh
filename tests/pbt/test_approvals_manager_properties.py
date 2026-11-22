"""Properties 7, 10, 11, 12, 13, 16, 22 and 24 of auto-approval, across the manager (task 9.1).

Temporary stores, a fake clock, ``asyncio.run`` inside plain tests, ``deadline=None``. The examples are
random interleavings (random ``sleep(0)`` counts and store write delays), not hand-picked orders.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from approvals_helpers import make_manager, open_incident  # noqa: E402

from chimericmesh.approvals.errors import RuleConflict, RuleNotFound  # noqa: E402
from chimericmesh.approvals.models import RuleFields, RulePatch  # noqa: E402
from chimericmesh.approvals.rulebook import load_rules, load_state  # noqa: E402
from chimericmesh.approvals.scope import check_files  # noqa: E402
from chimericmesh.services.limiter import ReduceAuditLimiter  # noqa: E402
from chimericmesh.store.errors import NotFound  # noqa: E402

SETTINGS = settings(max_examples=20, deadline=None, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
GOOD = {"kind": "code_change", "files": ["upstream.py"], "estimated_lines": 10, "verification": "run tests"}
WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu"]
CONTROL = re.compile("[\\r\\n\\x00]")


async def incidents(rig, n):
    return [await open_incident(rig.hub, message=f"disk full on volume {WORDS[i]} please") for i in range(n)]


def delayed_store(rig, delays):
    """Wrap ``store.transact`` so each call first yields a random number of times."""
    real = rig.store.transact
    state = {"n": 0}

    async def slow(fn):
        for _ in range(delays[state["n"] % len(delays)]):
            await asyncio.sleep(0)
        state["n"] += 1
        return await real(fn)

    rig.store.transact = slow
    return real


# ---- Property 7: hourly quota under concurrency ------------------------------------------------------------
@SETTINGS
@given(n=st.integers(2, 8), cap=st.integers(1, 5), delays=st.lists(st.integers(0, 6), min_size=1, max_size=4),
       offsets=st.lists(st.integers(0, 4), min_size=8, max_size=8))
def test_property_7_hourly_quota_holds_under_concurrency(n, cap, delays, offsets):
    rig = make_manager("store")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=cap), actor="a")
        ids = await incidents(rig, n)
        delayed_store(rig, delays)

        async def create(i, pause):
            for _ in range(pause):
                await asyncio.sleep(0)
            return await rig.hub.create_proposal(i, "fix", extra=GOOD)

        out = await asyncio.gather(*(create(i, offsets[k]) for k, i in enumerate(ids)))
        assert len([p for p in out if p.status == "approved"]) == min(n, cap)
        assert all(p.status in ("approved", "pending") for p in out)
        assert rig.manager._ledger.usage("r1", "x", 0, rig.clock.t).hour == min(n, cap)

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 10: revocation is immediate and total ---------------------------------------------------------
@SETTINGS
@given(before=st.integers(0, 3), during=st.integers(0, 3), after=st.integers(0, 3), revoke_at=st.integers(0, 6),
       delays=st.lists(st.integers(0, 6), min_size=1, max_size=4))
def test_property_10_a_revoked_rule_leaves_nothing_approved(before, during, after, revoke_at, delays):
    rig = make_manager("store")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=10, max_per_incident=3),
                                      actor="a")
        ids = await incidents(rig, max(before + during + after, 1))
        for i in ids[:before]:
            await rig.hub.create_proposal(i, "early", extra=GOOD)
        delayed_store(rig, delays)

        async def create(i, pause):
            for _ in range(pause):
                await asyncio.sleep(0)
            return await rig.hub.create_proposal(i, "fix", extra=GOOD)

        async def revoke():
            for _ in range(revoke_at):
                await asyncio.sleep(0)
            return await rig.manager.revoke_rule("r1", actor="a", key="k")

        tasks = [asyncio.ensure_future(create(i, k)) for k, i in enumerate(ids[before:before + during])]
        rev = asyncio.ensure_future(revoke())
        await asyncio.gather(*tasks, rev)
        late = [await rig.hub.create_proposal(i, "late", extra=GOOD) for i in ids[before + during:]]
        assert rev.result().status == "revoked"
        for prop in rig.hub.list_proposals():
            assert not (prop.approved_by_rule == "r1" and prop.status == "approved"), prop.id
        assert all(p.status == "pending" and p.approved_by_rule is None for p in late)

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 13: pause, revoke and withdraw are never refused -------------------------------------------------------
OPS13 = st.lists(st.tuples(st.sampled_from(["revoke", "pause_rule", "pause_all", "withdraw", "enable"]),
                           st.integers(0, 4)), min_size=1, max_size=25)


@SETTINGS
@given(OPS13)
def test_property_13_reduce_operations_never_raise_and_never_loosen(ops):
    rig = make_manager("store")
    rig.manager._limiter = ReduceAuditLimiter(rig.store.audit, now=rig.clock)

    async def go():
        await rig.add_service()
        for i in range(3):
            await rig.manager.create_rule(RuleFields("api-gw", "signature", f"sig {i}"), actor="a")
        for i in await incidents(rig, 3):
            await rig.hub.create_proposal(i, "fix")
        rank = {"active": 0, "suspended": 1, "revoked": 2, "expired": 2}
        seen: dict[str, str] = {}
        for op, n in ops:
            rid, pid = f"r{n % 4 + 1}", f"p{n % 4 + 1}"
            try:
                if op == "revoke":
                    await rig.manager.revoke_rule(rid, actor="n", key="same")
                elif op == "pause_rule":
                    await rig.manager.pause_rule(rid, actor="n", key="same")
                elif op == "pause_all":
                    await rig.manager.pause(actor="n", key="same")
                elif op == "withdraw":
                    await rig.manager.withdraw(pid, actor="n", key="same")
                else:                                    # a widen: it may be refused; the reduce operations may not
                    await rig.manager.enable_rule(rid, actor="admin")
            except (RuleNotFound, NotFound):
                assert (op in ("revoke", "pause_rule", "enable") and rid == "r4") or (op == "withdraw" and pid == "p4")
            except RuleConflict:
                assert op == "enable"
            for rule in rig.manager.rules():
                if op != "enable":
                    assert rank[rule.status] >= rank[seen.get(rule.id, "active")]
                seen[rule.id] = rule.status
        await rig.manager._limiter.aclose()
        actions = await rig.actions()
        reduce_rows = sum(1 for a in actions if a in ("rule_revoked", "rule_paused", "rules_paused",
                                                      "proposal_withdrawn"))
        assert reduce_rows <= 10                          # one key: ten rows a window, the rest coalesced

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 16: completion check ---------------------------------------------------------------------------------------
FILE_POOL = ["upstream.py", "lib/util.py", "../outside.py", ".git/hooks/x.py", "settings.json", "k.pem", "sub/a.php",
             "f1.py", "f2.py", "f3.py", "f4.py", "f5.py", "f6.py"]


@SETTINGS
@given(files=st.lists(st.sampled_from(FILE_POOL), max_size=7, unique=True),
       lines=st.one_of(st.none(), st.integers(0, 300)),
       result=st.sampled_from([None, "passed", "failed", "not_run"]), max_files=st.integers(1, 6),
       max_lines=st.integers(1, 200))
def test_property_16_completion_suspends_exactly_on_a_violation(files, lines, result, max_files, max_lines):
    rig = make_manager("bare")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_files=max_files,
                                                 max_lines=max_lines), actor="a")
        (iid,) = await incidents(rig, 1)
        prop = await rig.hub.create_proposal(iid, "fix", extra={**GOOD, "estimated_lines": min(5, max_lines)})
        assert prop.status == "approved"
        extra: dict = {}
        if files:
            extra["files_changed"] = files
        if lines is not None:
            extra["lines_changed"] = lines
        if result is not None:
            extra["verification_result"] = result
        done = await rig.hub.complete(prop.id, "x", extra=extra or None)
        assert done is not None and done.status == "done"          # the completion itself is never refused
        bad_files = bool(files) and bool(check_files(rig.folder, files))
        violation = (bad_files or len(files) > max_files or (lines is not None and lines > max_lines)
                     or result == "failed")
        assert (rig.manager.rule("r1").status == "suspended") == violation, (files, lines, result)

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 22: audit hygiene ----------------------------------------------------------------------------------------------
JUNK = st.text(alphabet=st.sampled_from(["\r", "\n", "\x00", "\x1b", "x", "y", " "]), max_size=30)


@SETTINGS
@given(sig=st.text(alphabet="abcdefgh ", min_size=1, max_size=20), junk=JUNK)
def test_property_22_canaries_stay_out_of_audit_rows_and_announcements(sig, junk):
    rig = make_manager("store")
    sig_canary = "SIGCANARY" + sig.strip().replace(" ", "")
    token_canary = "TOKENCANARY-99"
    rig.store.add_redaction(token_canary)

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "signature", sig_canary, note="note" + junk), actor="a")
        iid = await open_incident(rig.hub)
        await rig.hub.create_proposal(iid, f"fix {junk} {token_canary}", extra=GOOD)
        await rig.manager.revoke_rule("r1", actor="bob" + junk, key="k")
        await rig.manager.pause(actor="irc:bob", key="k")
        rows = await rig.audit()
        text = " ".join(str(v) for row in rows for v in row.values())
        assert sig_canary not in text and token_canary not in text
        for row in rows:
            for value in row.values():
                assert not CONTROL.search(str(value))
        for line in rig.announced + [m.text for m in rig.hub.transcript]:
            assert not CONTROL.search(line), line
            assert sig_canary not in line

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 24: a reduce is never undone by a widen or a reload -----------------------------------------------------------------
OPS24 = st.lists(st.tuples(st.sampled_from(["enable", "patch", "reload", "revoke", "sweep", "resume", "pause_all",
                                            "pause_rule"]), st.integers(0, 5)), min_size=1, max_size=10)


@SETTINGS
@given(ops=OPS24, delays=st.lists(st.integers(0, 5), min_size=1, max_size=4), start_suspended=st.booleans())
def test_property_24_a_reduce_is_never_undone(ops, delays, start_suspended):
    rig = make_manager("store")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "signature", "disk full"), actor="a")
        if start_suspended:
            await rig.manager.pause_rule("r1", actor="a", key="k")
        await rig.manager.pause(actor="a", key="k")
        delayed_store(rig, delays)

        async def run(op, pause):
            for _ in range(pause):
                await asyncio.sleep(0)
            try:
                if op == "enable":
                    await rig.manager.enable_rule("r1", actor="admin")
                elif op == "patch":
                    await rig.manager.patch_rule("r1", RulePatch(max_lines=7), actor="admin")
                elif op == "reload":
                    await rig.manager._reload()
                elif op == "revoke":
                    await rig.manager.revoke_rule("r1", actor="a", key="k")
                elif op == "sweep":
                    await rig.manager.sweep_once()
                elif op == "resume":
                    await rig.manager.resume(actor="admin")
                elif op == "pause_all":
                    await rig.manager.pause(actor="a", key="k")
                else:
                    await rig.manager.pause_rule("r1", actor="a", key="k")
            except (RuleConflict, RuleNotFound):
                pass

        await asyncio.gather(*(run(op, n) for op, n in ops))
        await rig.manager.sweep_once()
        db = {r.id: r for r in await rig.store.read(load_rules)}
        paused_db = (await rig.store.read(load_state))[0]
        mine = rig.manager.rule("r1")
        if any(op == "revoke" for op, _ in ops):
            assert mine.status == "revoked" and db["r1"].status == "revoked"
        assert mine.status == db["r1"].status                    # mirror and database agree after one sweep
        assert rig.manager.paused == paused_db
        if not any(op in ("resume", "pause_all") for op, _ in ops):
            assert rig.manager.paused                           # the initial pause survives every other operation

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 11: denial transitions only for rule approvals ---------------------------------------------------
OPS11 = st.lists(st.sampled_from(["user", "rule", "revoke", "pause_rule", "pause_all", "sweep", "advance",
                                  "withdraw"]), min_size=1, max_size=12)


@SETTINGS
@given(OPS11)
def test_property_11_only_rule_approvals_are_denied_by_the_hub(ops):
    rig = make_manager("bare")

    async def go():
        await rig.add_service()
        await rig.manager.create_rule(RuleFields("api-gw", "category", "disk", max_per_hour=10, max_per_incident=3),
                                      actor="a")
        ids = await incidents(rig, 12)
        used = 0
        for op in ops:
            if op in ("user", "rule", "withdraw") and used < len(ids):
                iid = ids[used]
                used += 1
                if op == "user":
                    prop = await rig.hub.create_proposal(iid, "manual", extra={"estimated_lines": 999})
                    await rig.hub.decide(prop.id, True, "alice")
                elif op == "rule":
                    await rig.hub.create_proposal(iid, "auto", extra=GOOD)
                else:
                    prop = await rig.hub.create_proposal(iid, "pending", extra={"estimated_lines": 999})
                    await rig.manager.withdraw(prop.id, actor="bob", key="k")
            elif op == "revoke":
                await rig.manager.revoke_rule("r1", actor="a", key="k")
            elif op == "pause_rule":
                await rig.manager.pause_rule("r1", actor="a", key="k")
            elif op == "pause_all":
                await rig.manager.pause(actor="a", key="k")
            elif op == "sweep":
                await rig.manager.sweep_once()
            elif op == "advance":
                rig.clock.advance(1801)
        await rig.manager.sweep_once()
        for prop in rig.hub.list_proposals():
            if prop.decision_origin == "hub":
                if prop.decided_by.startswith(("rule-", "rule:")):
                    assert prop.approved_by_rule is not None, (prop.id, prop.decided_by)
                else:
                    assert prop.decided_by.startswith(("withdrawn:", "expired:")), prop.decided_by
            if prop.approved_by_rule is None and prop.status in ("approved", "done"):
                assert prop.decided_by == "alice" and prop.decision_origin == "user"

    try:
        asyncio.run(go())
    finally:
        rig.close()


# ---- Property 12: reserved prefixes are never accepted from callers ----------------------------------------------
RESERVED = ["rule:", "rule-", "withdrawn:", "expired:", "expired-"]


def vary(prefix: str, mask: list[bool]) -> str:
    return "".join(c.upper() if mask[i % len(mask)] else c for i, c in enumerate(prefix))


@SETTINGS
@given(prefix=st.sampled_from(RESERVED), mask=st.lists(st.booleans(), min_size=1, max_size=6),
       spaces=st.integers(0, 3), suffix=st.text(alphabet="abcxyz019-", max_size=6))
def test_property_12_reserved_prefixes_are_refused_on_decide(prefix, mask, spaces, suffix):
    from chimericmesh.api import create_app
    from chimericmesh.hub import Hub

    tmp = tempfile.TemporaryDirectory()
    saved = os.environ.get("CHIMERICMESH_HOME")
    os.environ["CHIMERICMESH_HOME"] = os.path.join(tmp.name, "home")
    hub = Hub()
    app = create_app(hub, start_listeners=False, enforce_access=False)

    async def go():
        iid = await open_incident(hub)
        prop = await hub.create_proposal(iid, "fix")
        by = (" " * spaces + vary(prefix, mask) + suffix)[:30]
        transport = httpx.ASGITransport(app=app, client=("7.7.7.7", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            reply = await c.post(f"/api/proposals/{prop.id}/decide", json={"approve": True, "by": by})
            return reply, hub.get_proposal(prop.id)

    try:
        reply, prop = asyncio.run(go())
        assert reply.status_code == 422 and prop.status == "pending" and prop.approved_by_rule is None
    finally:
        app.state.store.close()
        if saved is None:
            os.environ.pop("CHIMERICMESH_HOME", None)
        else:
            os.environ["CHIMERICMESH_HOME"] = saved
        tmp.cleanup()
