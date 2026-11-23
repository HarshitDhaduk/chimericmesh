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


# ---- 5.1 services list ---------------------------------------------------------------------------------

def test_services_list_task_and_route():
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["services-list"] == {"id": "services-list", "every": "5000", "view": "services"}
    assert "request('GET', '/api/services')" in code("loadServices")


def test_card_renderer_writes_the_fields_as_text():
    card = code("serviceCard")
    for piece in ("stateLabel('service'", "relTime(", "error_rate", "open_incidents", "stateLabel('source'",
                  "lines_dropped", "last_error", "el("):
        assert piece in card, piece
    assert "'built in'" in card
    assert re.search(r"if \(!s\.builtin\)", card), "the built-in card has no Edit control"


def test_empty_state_text_and_buttons():
    script = ds.strip_comments(ds.script_text())
    assert script.count("No services yet. Run discovery or add one.") == 1
    empty = code("renderServices")
    assert "'Discover local apps'" in empty and "'Add service'" in empty


def test_sort_and_labels_have_cases():
    names = case_names()
    assert names.count("sortServices") >= 3
    text = cases_text()
    for label in ("active", "idle", "never seen", "running", "missing", "unreadable", "error", "stopped", "unknown"):
        assert f"expect: '{label}'" in text, label


def test_services_code_calls_no_discovery_route():
    for name in ("loadServices", "renderServices", "serviceCard", "renderServiceForm", "saveService",
                 "loadServiceDetail", "renderServiceHeader"):
        assert "/api/discovery" not in fns().get(name, ""), name  # existence is checked by the 5.0 to 5.2 tests


def test_services_css_is_one_column_on_small_screens():
    page = ds.load_page()
    assert "grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr))" in page
    assert re.search(r"\.svc-card[^{]*\{[^}]*overflow-wrap: anywhere", page)


# ---- 5.2 add and edit form -----------------------------------------------------------------------------

def test_form_fields_have_labels():
    form = code("renderServiceForm")
    for label in ("'Id'", "'Name'", "'Hosts (comma separated)'", "'App names (comma separated)'", "'Code folder'"):
        assert label in form, label
    assert "field(" in form and "uiConfig.protocols" in form and "uiConfig.formats" in code("sourceRow")
    f = code("field")
    assert "htmlFor" in f and "el('label'" in f
    row = code("sourceRow")
    for piece in ("'Path'", "'Format'", "'Enabled'", "'Remove'"):
        assert piece in row, piece
    assert "if (create)" in form  # the id field exists on create only


def test_save_runs_the_steps_in_order_and_stops_at_the_first_failure():
    save = code("saveService")
    i_create = save.index("request('POST', '/api/services'")
    i_patch = save.index("request('PATCH', `/api/services/${")
    i_diff = save.index("diffSources(")
    assert i_create < i_diff and i_patch < i_diff
    for step in ("request('POST', `/api/services/${", "request('PATCH', `/api/services/${", "request('DELETE', `/api/services/${"):
        assert step in save, step
    assert "errorText(" in save and "textContent" in save
    assert "return;" in save  # stops at the first failure
    assert "mode = 'edit'" in save  # a created service keeps the form in edit mode
    assert "servicePatch(" in save
    assert "actionButton('svc-save'" in code("renderServiceForm")


def test_form_helpers_have_cases():
    names = case_names()
    for fn in ("diffSources", "buildAcceptBody", "servicePatch", "splitList"):
        assert names.count(fn) >= 2, fn
    text = cases_text()
    assert "code_folder: null" in text  # a cleared code folder is sent as null
