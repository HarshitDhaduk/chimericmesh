"""Property 18: audit append-only, capped, control-free."""
import asyncio
import re
import sqlite3

from hypothesis import given, settings, strategies as st

from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# every code point, including control characters and lone surrogates
text = st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF, blacklist_categories=()),
               max_size=1500)
entry = st.tuples(text, text, text, text)


@settings(max_examples=60, deadline=None)
@given(st.lists(entry, min_size=1, max_size=8))
def test_property_18_audit_append_only(entries):
    store = Store.open(":memory:", registry=core_registry())
    try:
        async def go():
            ids = [await store.audit.append(a, b, c, d) for a, b, c, d in entries]
            return ids, await store.audit.read(limit=500)

        ids, rows = asyncio.run(go())
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert [r["id"] for r in rows] == ids[::-1]
        for r in rows:
            assert len(r["actor"]) <= 100 and len(r["action"]) <= 100 and len(r["subject"]) <= 100
            assert len(r["detail"]) <= 1000
            for key in ("actor", "action", "subject", "detail"):
                assert not CONTROL.search(r[key])
                r[key].encode("utf-8")  # no lone surrogate survives
        for sql in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"):
            try:
                store.transact_sync(lambda tx, sql=sql: tx.execute(sql))
            except sqlite3.Error:
                pass
            else:
                raise AssertionError("audit_log accepted " + sql)
        assert len(asyncio.run(store.audit.read(limit=500))) == len(entries)
    finally:
        store.close()
