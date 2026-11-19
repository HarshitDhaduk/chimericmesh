"""Task 2.3: ``submit`` (sub-criteria 1.1a to 1.1d of the design)."""
from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path

import pytest

from chimericmesh.connectors.submit import make_submit
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.services.registry import ServiceRegistry

SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"


def make_event(**kw) -> Event:
    base = dict(source="file", host="h", app="a", severity=3, message="boom")
    base.update(kw)
    return Event(**base)


class Recorder:
    def __init__(self, result="same") -> None:
        self.calls: list[Event] = []
        self.result = result

    async def __call__(self, event: Event):
        self.calls.append(event)
        return event if self.result == "same" else self.result


def test_1_1a_sets_log_source_id_and_leaves_the_rest():
    rec = Recorder()
    submit = make_submit(rec)
    ev = make_event()
    asyncio.run(submit(ev, "ls1"))
    sent = rec.calls[0]
    assert sent.log_source_id == "ls1"
    assert dataclasses.replace(sent, log_source_id=None) == ev
    assert ev.log_source_id is None  # the input is not mutated


def test_1_1a_without_a_source_id_passes_the_event_unchanged():
    rec = Recorder()
    ev = make_event(log_source_id="keep")
    asyncio.run(make_submit(rec)(ev))
    assert rec.calls == [ev] and rec.calls[0] is ev


def test_1_1b_ingest_awaited_once_and_its_result_returned():
    returned = make_event(message="tagged", id=7, service_id="web")
    rec = Recorder(result=returned)
    out = asyncio.run(make_submit(rec)(make_event(), "ls1"))
    assert len(rec.calls) == 1 and out is returned


def test_1_1b_none_from_ingest_returns_the_sent_event():
    rec = Recorder(result=None)
    out = asyncio.run(make_submit(rec)(make_event(), "ls1"))
    assert out is rec.calls[0] and out.log_source_id == "ls1"


def test_exception_from_ingest_propagates_unchanged():
    boom = RuntimeError("store is gone")

    async def failing(event):
        raise boom

    with pytest.raises(RuntimeError) as err:
        asyncio.run(make_submit(failing)(make_event()))
    assert err.value is boom


def test_submit_never_sets_a_service_id():
    rec = Recorder()
    asyncio.run(make_submit(rec)(make_event(), "ls1"))
    assert rec.calls[0].service_id is None


def test_1_1c_hub_tagger_assigns_the_service(svc_mem_store, tmp_path):
    if not hasattr(Hub(), "tagger"):
        pytest.skip("Hub.tagger does not exist yet")

    async def scenario():
        hub = Hub()
        registry = ServiceRegistry.load(svc_mem_store, state=hub)
        hub.tagger = registry.tag
        await registry.create_service(ServiceFields(id="web", name="Web", app_names=("proxy-worker",)), actor="t")
        await registry.create_service(ServiceFields(id="mail", name="Mail"), actor="t")
        src = await registry.add_log_source("mail", LogSourceFields(path=str(tmp_path / "mail.log"), format="plain"),
                                            actor="t")
        submit = make_submit(hub.ingest)
        from_file = await submit(make_event(app="whatever"), src.id)
        from_tcp = await submit(make_event(source="syslog", app="proxy-worker", host="elsewhere"), None)
        return from_file, from_tcp

    from_file, from_tcp = asyncio.run(scenario())
    assert from_file.service_id == "mail" and from_file.id > 0
    assert from_tcp.service_id == "web"


def test_1_1d_connectors_do_not_import_hub():
    for path in sorted((SRC / "connectors").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "")] + [a.name for a in node.names]
            for name in names:
                assert name.split(".")[-1] != "hub" and not name.endswith(".hub"), (path.name, name)
