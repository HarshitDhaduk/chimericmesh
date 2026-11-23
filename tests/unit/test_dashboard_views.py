"""Static checks of the Services views (dashboard task 5; R3.1 to R3.7, R6.1, R8.2, Success Criterion 1).

Runtime behaviour of the pure helpers is covered by #selftest cases (Manual, headless Edge); these tests read
the page source.
"""
from __future__ import annotations

import re

import dashboard_static as ds
from test_dashboard_core import case_names, cases_text, pure_block


def fns() -> dict[str, str]:
    return ds.functions(ds.script_text())


def code(name: str) -> str:
    return ds.strip_comments(fns()[name])


def attrs_by_id() -> dict[str, dict]:
    return {a.get("id"): a for t, a in ds.html_parser().tags if a.get("id")}


def sched_rows() -> list[dict[str, str]]:
    sched = ds.strip_comments(ds.section("SCHEDULER"))
    rows = re.findall(r"\{ id: '([a-z-]+)', every: (\d+), last: 0, view: '([a-z*]+)'", sched)
    return [{"id": i, "every": e, "view": v} for i, e, v in rows]


def segment(start_marker: str, end_marker: str) -> str:
    page = ds.load_page()
    a = page.index(start_marker)
    return page[a: page.index(end_marker, a)]


# ---- 5.0 service detail skeleton and route ------------------------------------------------------------

def test_detail_route_has_a_self_test_case():
    assert re.search(r"name: 'parseRoute', args: \['#/services/web'\], expect: \{ view: 'service', id: 'web' \}",
                     cases_text())


def test_one_detail_container_with_header_and_named_slots():
    page = ds.load_page()
    assert page.count('id="view-service"') == 1
    assert re.search(r'<div id="view-service" data-view="service" hidden>', page)
    seg = segment('id="view-service"', "<!-- /view-service -->")
    for ident in ("svcName", "svcId", "svcState", "svcSeen", "svcRate", "svcOpen", "svcGone", "svcBack"):
        assert f'id="{ident}"' in seg, ident
    slots = re.findall(r'data-slot="([a-z-]+)"', seg)
    assert slots == ["permissions", "live-log", "incidents", "proposals"]
    assert "service no longer exists" in seg


def test_detail_fetches_the_service_with_an_encoded_id_and_handles_404_without_a_toast():
    body = code("loadServiceDetail")
    assert "request('GET', `/api/services/${encodeURIComponent(" in body
    assert "e.status === 404" in body and "showServiceGone(" in body
    gone = code("showServiceGone")
    assert "toast(" not in gone and "svcGone" in gone
    assert "ENTER.service" in ds.strip_comments(ds.script_text())


def test_detail_scheduler_task_runs_only_on_the_detail_view():
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["service-detail"]["view"] == "service" and rows["service-detail"]["every"] == "5000"
