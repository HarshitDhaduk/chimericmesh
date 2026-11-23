"""AgentService (task 7.1): the facade, run against a bare hub and a store-backed hub (review M2)."""
from __future__ import annotations
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import asyncio
import os
import threading
import time
import pytest
from agent_helpers import make_agent
from chimericmesh.agent import service as service_module
from chimericmesh.agent.codesearch import Hit, SearchResult
from chimericmesh.agent.errors import NoCodeFolder, SearchBusy, UnknownProposal, UnknownService
MODES = ["bare", "store"]
@pytest.fixture(params=MODES)
def ar(request):
    rig = make_agent(request.param, workspace_root=None)
    yield rig
    rig.close()
def write(folder: str, name: str, text: str) -> str:
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
def test_service_events_digest_and_code_search_for_a_registered_service(ar):
    async def go():
        await ar.add_service()
        write(ar.folder, "app.py", "def disk_full():\n    raise OSError('disk full on /var')\n")
        await ar.incident()
        events = await ar.agent.service_events("api-gw", 25, 0, None)
        assert events["service_id"] == "api-gw" and len(events["events"]) == 3
        digest = await ar.agent.digest("api-gw", 15)
        assert digest["service_id"] == "api-gw" and digest["total_events"] == 3 and digest["minutes"] == 15
        found = await ar.agent.code_search("api-gw", "disk full")
        assert found["hits"] and found["hits"][0]["file"] == "app.py" and found["truncated"] is False
    asyncio.run(go())
def test_unassigned_and_unknown_ids_raise_unknown_service(ar):
    async def go():
        await ar.add_service()
        for sid in ("unassigned", "nope"):
            for call in (lambda: ar.agent.service_events(sid, 25, 0, None), lambda: ar.agent.digest(sid, 15),
                         lambda: ar.agent.code_search(sid, "x")):
                with pytest.raises(UnknownService):
                    await call()
            with pytest.raises(UnknownService):
                ar.agent.permissions_snippet(sid, None)
    asyncio.run(go())
def test_code_search_on_a_service_without_a_code_folder_raises(ar):
    async def go():
        await ar.add_service(folder=None)
        with pytest.raises(NoCodeFolder):
            await ar.agent.code_search("api-gw", "x")
        with pytest.raises(NoCodeFolder):
            ar.agent.permissions_snippet("api-gw", None)
    asyncio.run(go())
class Gate:
    """A fake ``search_code`` that blocks its worker thread until released."""
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.seen_cancelled: list[bool] = []
    def __call__(self, folder, query, *, protected_dirs=(), cancelled=lambda: False, **kw):
        self.started.release()
        self.release.wait(10)
        self.seen_cancelled.append(bool(cancelled()))
        return SearchResult((Hit("a.py", 1, query),), False)
async def wait_started(gate: Gate, n: int) -> None:
    for _ in range(n):
        for _ in range(200):
            if gate.started.acquire(blocking=False):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the search thread did not start")
def test_two_running_searches_and_a_third_gives_search_busy(ar, monkeypatch):
    gate = Gate()
    monkeypatch.setattr(service_module, "search_code", gate)
    async def go():
        await ar.add_service()
        a = asyncio.create_task(ar.agent.code_search("api-gw", "one"))
        b = asyncio.create_task(ar.agent.code_search("api-gw", "two"))
        await wait_started(gate, 2)
        with pytest.raises(SearchBusy) as busy:
            await ar.agent.code_search("api-gw", "three")
        assert busy.value.retry_after == 2
        gate.release.set()
        assert (await a)["hits"][0]["text"] == "one" and (await b)["hits"][0]["text"] == "two"
        assert (await ar.agent.code_search("api-gw", "four"))["hits"]
    asyncio.run(go())
def test_the_search_thread_is_told_to_stop_when_the_awaiting_task_is_cancelled(ar, monkeypatch):
    gate = Gate()
    monkeypatch.setattr(service_module, "search_code", gate)
    async def go():
        await ar.add_service()
        task = asyncio.create_task(ar.agent.code_search("api-gw", "one"))
        await wait_started(gate, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.release.set()
        for _ in range(100):
            if gate.seen_cancelled:
                break
            await asyncio.sleep(0.01)
        assert gate.seen_cancelled == [True]
    asyncio.run(go())
def test_a_cancelled_search_keeps_its_slot_until_the_thread_returns(ar, monkeypatch):
    gate = Gate()
    monkeypatch.setattr(service_module, "search_code", gate)
    async def go():
        await ar.add_service()
        a = asyncio.create_task(ar.agent.code_search("api-gw", "one"))
        b = asyncio.create_task(ar.agent.code_search("api-gw", "two"))
        await wait_started(gate, 2)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        with pytest.raises(SearchBusy):                    # the thread of ``a`` is still blocked
            await ar.agent.code_search("api-gw", "three")
        gate.release.set()
        await b
        for _ in range(200):                               # the slot is freed by the thread's completion
            try:
                assert (await ar.agent.code_search("api-gw", "five"))["hits"]
                break
            except SearchBusy:
                await asyncio.sleep(0.01)
        else:
            raise AssertionError("the slot was never freed")
    asyncio.run(go())
def test_verify_user_approved_proposal(ar):
    async def go():
        await ar.add_service()
        iid = await ar.incident()
        target = os.path.join(ar.folder, "upstream.py")
        prop = await ar.user_approved(iid, ["upstream.py"])          # declared relative to the code folder
        out = ar.agent.verify(prop.id, [target])
        assert out["ok"] is True and out["reasons"] == [] and out["status"] == "approved"
        assert out["approval"]["by"] == "user" and out["declared_files"] == ["upstream.py"]
        other = ar.agent.verify(prop.id, [os.path.join(ar.folder, "other.py")])
        assert other["ok"] is False and other["reasons"] == ["file 1 was not declared in the proposal"]
    asyncio.run(go())
def test_verify_rule_approved_then_revoked(ar):
    async def go():
        await ar.add_service()
        iid = await ar.incident()
        target = os.path.join(ar.folder, "upstream.py")
        rule, prop = await ar.rule_approved(iid, ["upstream.py"])
        out = ar.agent.verify(prop.id, [target])
        assert out["ok"] is True and out["approval"]["by"] == "rule" and out["approval"]["rule_id"] == rule.id
        await ar.manager.revoke_rule(rule.id, actor="a", key="k")
        again = ar.agent.verify(prop.id, [target])
        assert again["ok"] is False
        assert any("revoked" in r for r in again["reasons"])
    asyncio.run(go())
def test_verify_with_no_approvals_manager_fails_every_rule_approval():
    rig = make_agent("bare", approvals=False)
    try:
        async def go():
            await rig.add_service()
            iid = await rig.incident()
            _, prop = await _rule_prop(rig, iid)
            out = rig.agent.verify(prop.id, [os.path.join(rig.folder, "upstream.py")])
            assert out["ok"] is False and any("unknown" in r for r in out["reasons"])
        asyncio.run(go())
    finally:
        rig.close()
async def _rule_prop(rig, iid):
    # approve through a manager-backed twin: here the facade has none, the hub's own manager still approves
    return await rig.rule_approved(iid, ["upstream.py"])
def test_verify_for_an_unassigned_incident_adds_the_note_and_is_ok(ar):
    async def go():
        iid = await ar.incident()                       # no service registered: the incident is unassigned
        target = os.path.join(ar.folder, "x.py")
        prop = await ar.user_approved(iid, [target])
        out = ar.agent.verify(prop.id, [target])
        assert out["ok"] is True
        assert "no code folder, path scope not checked" in out["reasons"]
    asyncio.run(go())
def test_verify_unknown_proposal_raises(ar):
    with pytest.raises(UnknownProposal):
        ar.agent.verify("p404", ["/x"])
def test_verify_text_is_clean_and_capped(ar):
    async def go():
        await ar.add_service()
        iid = await ar.incident()
        name = "a" + "b" * 200 + ".py"
        prop = await ar.user_approved(iid, [name])
        out = ar.agent.verify(prop.id, [os.path.join(ar.folder, name)])
        assert out["ok"] is True and out["declared_files"] == [name]
        assert all(len(f) <= 500 and "\x00" not in f for f in out["declared_files"])
    asyncio.run(go())
def test_code_folder_for_incident_returns_the_live_folder_or_none(ar):
    async def go():
        await ar.add_service()
        iid = await ar.incident()
        inc = ar.hub.get_incident(iid)
        assert inc.service_id == "api-gw"
        assert ar.agent.code_folder_for_incident(inc) == ar.folder
        inc.service_id = None
        assert ar.agent.code_folder_for_incident(inc) is None
        inc.service_id = "gone"
        assert ar.agent.code_folder_for_incident(inc) is None
    asyncio.run(go())
def test_permissions_snippet_uses_the_workspace_root():
    rig = make_agent("bare")
    try:
        async def go():
            await rig.add_service()
            root = os.path.dirname(rig.folder)
            rig.agent._ports = type(rig.agent._ports)(
                state=rig.hub, registry=rig.registry, approvals=rig.manager, protected_dirs=(), workspace_root=root)
            text = rig.agent.permissions_snippet("api-gw", None)
            assert '"code/**"' in text and "Workspace root used:" in text
            assert "python -m pytest -q" in text
            custom = rig.agent.permissions_snippet("api-gw", "pytest -x")
            assert "pytest -x" in custom
        asyncio.run(go())
    finally:
        rig.close()
def test_aclose_closes_the_queue(ar):
    async def go():
        held = asyncio.create_task(ar.agent.watch.next(timeout=30, service_id=None, exclude=frozenset()))
        await asyncio.sleep(0.1)
        ar.agent.aclose()
        out = await asyncio.wait_for(held, 1)
        assert out["status"] == "timeout"
    asyncio.run(go())
def test_the_facade_takes_only_ports_and_no_limits_type():          # (R1-L1)
    import inspect
    from chimericmesh import agent
    from chimericmesh.agent.service import AgentService
    assert list(inspect.signature(AgentService.__init__).parameters) == ["self", "ports"]
    for module in (agent, agent.limits if hasattr(agent, "limits") else agent):
        assert not hasattr(module, "AgentLimits")
    import chimericmesh.agent.limits as limits
    assert not hasattr(limits, "AgentLimits")
def test_module_does_not_import_the_hub():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[-1] not in ("hub", "api"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.endswith(".hub") for a in node.names)
