"""Static checks of the page core: sections, pure block, self-test table, navigation (dashboard task 4.1).

The page is never executed here (no JavaScript runner). Runtime behaviour of the pure block is checked by
the in-page #selftest, a Manual step run with headless Edge (design, Testing Strategy).
"""
from __future__ import annotations

import re

import dashboard_static as ds

BANNERS = ["PURE", "SELFTEST", "DOM helpers", "API", "STATE", "SCHEDULER", "VIEWS", "INIT"]
FORBIDDEN_IN_PURE = [r"\bdocument\b", r"\bwindow\b", r"\bglobalThis\b", r"\bself\b", r"\btop\b", r"\bfetch\b",
                     r"\bsessionStorage\b", r"\blocalStorage\b", r"\bEventSource\b", r"\blocation\b",
                     r"\bnavigator\b", r"\bsetTimeout\b", r"Date\.now", r"new Date\(\)", r"Math\.random"]


def pure_block() -> str:
    s = ds.script_text()
    a, b = s.index("/* PURE:begin */"), s.index("/* PURE:end */")
    assert a < b
    return s[a + len("/* PURE:begin */"): b]


def cases_text() -> str:
    s = ds.script_text()
    a = s.index("const SELFTEST_CASES")
    return s[a: s.index("];", a) + 2]


def case_names() -> list[str]:
    return re.findall(r"name:\s*'([A-Za-z]+)'", cases_text())


# ---- structure ------------------------------------------------------------------------------------

def test_banners_exist_once_in_order():
    s = ds.script_text()
    pos = []
    for name in BANNERS:
        banner = f"/* ==== {name} ==== */"
        assert s.count(banner) == 1, name
        pos.append(s.index(banner))
    assert pos == sorted(pos)


def test_pure_markers_are_inside_the_pure_section():
    s = ds.script_text()
    assert s.index("/* ==== PURE ==== */") < s.index("/* PURE:begin */") < s.index("/* PURE:end */")
    assert s.index("/* PURE:end */") < s.index("/* ==== SELFTEST ==== */")


def test_selftest_cases_stay_under_two_hundred_lines():
    assert cases_text().count("\n") + 1 <= 200


def test_pure_block_touches_no_browser_global():
    body = ds.strip_comments(pure_block())
    for pat in FORBIDDEN_IN_PURE:
        assert not re.search(pat, body), pat


def test_every_pure_function_has_a_case():
    names = set(ds.functions(pure_block()))
    assert names, "no function in the pure block"
    missing = names - set(case_names())
    assert not missing, sorted(missing)
    unknown = set(case_names()) - names
    assert not unknown, f"cases for functions that are not in the pure block: {sorted(unknown)}"


def test_the_contract_functions_exist():
    names = set(ds.functions(pure_block()))
    need = {"parseRoute", "capText", "relTime", "countdown", "stateLabel", "nextBackoff", "errorText", "dueTasks",
            "sevName", "isNewer", "lockState", "afterAuthFailure"}
    assert need <= names, sorted(need - names)


def test_edge_value_cases_are_present():
    names = case_names()
    assert names.count("nextBackoff") >= 9  # 0 to 8
    assert names.count("capText") >= 3
    assert names.count("dueTasks") >= 4  # hidden tab, another view, view '*', due and not due
    text = cases_text()
    assert "'*'" in text and "false" in text  # a '*' task and a hidden tab


def test_selftest_mode_returns_before_any_request():
    init = ds.section("INIT")
    i = init.index("'#selftest'")
    for call in ("request(", "refresh(", "fetchChat(", "loadWorkflow(", "fetch("):
        j = init.find(call)
        assert j < 0 or j > i, call


def test_selftest_writes_with_text_content_to_one_element():
    s = ds.section("SELFTEST")
    assert "SELFTEST PASS" in s and "textContent" in s
    assert "SELFTEST FAIL" in s


# ---- navigation and containers ------------------------------------------------------------------

def tags():
    return ds.html_parser().tags


def test_nav_has_four_buttons_and_the_view_containers_exist():
    page = ds.load_page()
    nav = re.search(r'<nav aria-label="Views">(.*?)</nav>', page, re.S)
    assert nav, "nav aria-label=Views"
    assert len(re.findall(r"<button\b", nav.group(1))) == 4
    containers = {a.get("data-view") for t, a in tags() if t == "div" and a.get("data-view")}
    assert {"overview", "services", "discovery", "rules"} <= containers


def test_overview_content_is_inside_view_overview():
    page = ds.load_page()
    start = page.index('id="view-overview"')
    nxt = page.index('id="view-services"')
    seg = page[start:nxt]
    for ident in ("stats", "guide", "incPanel", "propPanel", "termPanel", "evPanel"):
        assert f'id="{ident}"' in seg, ident


def test_status_banner_and_toast():
    attrs = {a.get("id"): a for t, a in tags() if a.get("id")}
    assert attrs["status"].get("role") == "status"
    assert "pausedBanner" in attrs and "hidden" in attrs["pausedBanner"]


def test_exactly_one_set_interval_and_it_is_the_schedulers():
    code = ds.strip_comments(ds.script_text())
    assert len(re.findall(r"\bsetInterval\s*\(", code)) == 1
    assert re.search(r"\bsetInterval\s*\(", ds.strip_comments(ds.section("SCHEDULER")))


def test_scheduler_has_the_overview_tasks():
    sched = ds.strip_comments(ds.section("SCHEDULER"))
    assert re.search(r"every:\s*2500", sched) and re.search(r"every:\s*1000", sched)
    assert "dueTasks(" in sched
