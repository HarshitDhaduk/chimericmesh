"""Property 19: no spurious notifications."""
from __future__ import annotations

import asyncio
import os
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.hub import Hub
from chimericmesh.services.models import LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.schema import services_registry
from chimericmesh.store import Store

names = st.sampled_from(["x", "y", "z"])
lists = st.sampled_from([(), ("h1",), ("h1", "h2"), ("h3",)])


@settings(max_examples=40, deadline=None)
@given(name=names, hosts=lists, apps=lists, protocols=st.sampled_from([(), ("irc",), ("file", "telnet")]),
       change=st.sets(st.sampled_from(["name", "hosts", "app_names", "protocols"])),
       src_change=st.sets(st.sampled_from(["format", "enabled", "backfill_lines", "host"])))
def test_property_19_no_spurious_notifications(name, hosts, apps, protocols, change, src_change):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(os.path.join(tmp, "n.db"), registry=services_registry())
        try:
            reg = ServiceRegistry.load(store, state=Hub(store=store), protected=[], home=os.path.join(tmp, "nohome"))
            calls = {"changed": 0, "commit": 0}

            def counts(sql):
                return store.read_sync(lambda tx: tx.one(sql)[0], allow_blocking=True)

            async def go():
                await reg.create_service(ServiceFields(id="s", name=name, hosts=hosts, app_names=apps, protocols=protocols), actor="t")
                src = await reg.add_log_source("s", LogSourceFields(path=os.path.join(tmp, "a.log"), format="plain", host="hh"), actor="t")
                reg.subscribe(on_service_changed=lambda tx, ch: calls.__setitem__("changed", calls["changed"] + 1),
                              after_commit=lambda ch: calls.__setitem__("commit", calls["commit"] + 1))
                rows = counts("SELECT COUNT(*) FROM audit_log")
                # service edit: every field equal unless chosen to differ
                alt = {"name": name + "2", "hosts": hosts + ("new-host",), "app_names": apps + ("new-app",),
                       "protocols": () if protocols else ("irc",)}
                cur = {"name": name, "hosts": hosts, "app_names": apps, "protocols": protocols}
                patch = ServicePatch(**{k: (alt[k] if k in change else cur[k]) for k in cur})
                before = dict(calls)
                await reg.update_service("s", patch, actor="t")
                if change:
                    assert calls == {"changed": before["changed"] + 1, "commit": before["commit"] + 1}
                    assert counts("SELECT COUNT(*) FROM audit_log") == rows + 1
                else:
                    assert calls == before and counts("SELECT COUNT(*) FROM audit_log") == rows
                # log source edit
                rows = counts("SELECT COUNT(*) FROM audit_log")
                cur_s = {"format": "plain", "enabled": True, "backfill_lines": 0, "host": "hh"}
                alt_s = {"format": "ncsa", "enabled": False, "backfill_lines": 5, "host": "other"}
                sp = LogSourcePatch(**{k: (alt_s[k] if k in src_change else cur_s[k]) for k in cur_s})
                before = dict(calls)
                await reg.update_log_source("s", src.id, sp, actor="t")
                if src_change:
                    assert calls == {"changed": before["changed"] + 1, "commit": before["commit"] + 1}
                    assert counts("SELECT COUNT(*) FROM audit_log") == rows + 1
                else:
                    assert calls == before and counts("SELECT COUNT(*) FROM audit_log") == rows

            asyncio.run(go())
        finally:
            store.close()
