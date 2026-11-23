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


# ---- task 4.2: token handling and the lock registry --------------------------------------------------------

PRIVILEGED_EXPECTED = [
    ("POST", "/api/services"), ("PATCH", "/api/services/web"), ("DELETE", "/api/services/web"),
    ("POST", "/api/services/web/log-sources"), ("PATCH", "/api/services/web/log-sources/ls-1"),
    ("DELETE", "/api/services/web/log-sources/ls-1"),
    ("POST", "/api/discovery/scan"), ("GET", "/api/discovery/scans/s1"),
    ("POST", "/api/discovery/suggestions/g1/accept"), ("POST", "/api/discovery/suggestions/g1/dismiss"),
    ("GET", "/api/discovery/dismissals"), ("DELETE", "/api/discovery/dismissals"),
    ("POST", "/api/rules"), ("PATCH", "/api/rules/r1"), ("POST", "/api/rules/r1/enable"), ("POST", "/api/rules/resume"),
    ("POST", "/api/proposals/p1/rule"), ("POST", "/api/proposals/p1/decide"), ("POST", "/api/terminal"),
]
TOKEN_FREE = [
    ("POST", "/api/rules/pause"), ("POST", "/api/rules/r1/pause"), ("POST", "/api/rules/r1/revoke"),
    ("POST", "/api/proposals/p1/withdraw"),
    ("GET", "/api/services"), ("GET", "/api/services/web"), ("GET", "/api/incidents"), ("GET", "/api/incidents/3"),
    ("GET", "/api/proposals"), ("GET", "/api/proposals/p1"), ("GET", "/api/proposals/p1/rule-defaults"),
    ("GET", "/api/rules"), ("GET", "/api/rules/r1"), ("GET", "/api/audit"), ("GET", "/api/approvals/status"),
    ("GET", "/api/stats"), ("GET", "/api/events"), ("GET", "/api/workflow"), ("GET", "/api/chat"),
    ("GET", "/api/ui-config"), ("GET", "/api/services/web/stream"), ("POST", "/api/ingest"),
    ("POST", "/api/demo/storm"), ("POST", "/api/proposals"), ("POST", "/api/proposals/p1/complete"),
    ("POST", "/api/services/web"), ("GET", "/api/rules/resume"), ("POST", "/api/rules/r1/pause/x"),
    ("POST", "/api/servicesX"), ("POST", "/api/terminal/x"),
]
ACTIONS_YES = {"svc-save", "svc-source-edit", "scan-start", "suggestion-accept", "suggestion-dismiss",
               "dismissals-clear", "proposal-approve", "proposal-deny", "rule-create", "rule-edit", "rule-enable",
               "rules-resume", "rule-from-proposal"}
ACTIONS_NO = {"rules-pause-all", "rule-pause", "rule-revoke", "proposal-withdraw"}
ACTIONS_COND = {"terminal-send"}


def privileged_rows() -> list[tuple[str, str]]:
    pure = pure_block()
    m = re.search(r"const PRIVILEGED = \[(.*?)\n\];", pure, re.S)
    assert m, "PRIVILEGED table"
    return re.findall(r"\[\s*'([A-Z]+)'\s*,\s*'([^']+)'\s*\]", m.group(1))


def needs_token(method: str, path: str) -> bool:
    return any(m == method and re.search(rx, path) for m, rx in privileged_rows())


def actions() -> dict[str, str]:
    m = re.search(r"const ACTIONS = \{(.*?)\n\};", pure_block(), re.S)
    assert m, "ACTIONS registry"
    return dict(re.findall(r"'([a-z-]+)'\s*:\s*(true|false|'conditional')", m.group(1)))


def test_privileged_table_matches_the_design():
    rows = privileged_rows()
    assert len(rows) == len(PRIVILEGED_EXPECTED)
    for method, path in PRIVILEGED_EXPECTED:
        assert needs_token(method, path), (method, path)
    for method, path in TOKEN_FREE:
        assert not needs_token(method, path), (method, path)
    for _, rx in rows:
        assert rx.startswith("^") and rx.endswith("$"), rx


def test_privileged_table_matches_the_servers_admin_routes():
    """Every route of the app that depends on require_admin is in the table (terminal is conditional)."""
    from approvals_helpers import collect_garbage  # noqa: F401
    from chimericmesh.api import create_app
    from chimericmesh.hub import Hub

    app = create_app(Hub(), start_listeners=False)
    try:
        admin_routes = set()
        for r in app.routes:
            deps = getattr(getattr(r, "dependant", None), "dependencies", [])
            if any("admin" in getattr(d.call, "__qualname__", "") for d in deps):
                for m in r.methods:
                    admin_routes.add((m, r.path))
    finally:
        app.state.store.close()
    sample = {"{service_id}": "web", "{source_id}": "ls-1", "{scan_id}": "s1", "{suggestion_id}": "g1",
              "{rule_id}": "r1", "{pid}": "p1"}
    assert admin_routes, "no admin route found"
    for method, path in admin_routes:
        for k, v in sample.items():
            path = path.replace(k, v)
        assert needs_token(method, path), (method, path)


def test_actions_registry_is_the_designs():
    a = actions()
    assert {k for k, v in a.items() if v == "true"} == ACTIONS_YES
    assert {k for k, v in a.items() if v == "false"} == ACTIONS_NO
    assert {k for k, v in a.items() if v == "'conditional'"} == ACTIONS_COND


def test_pure_token_functions_have_cases():
    names = case_names()
    for fn in ("needsToken", "actionNeedsToken", "errorText", "lockState", "afterAuthFailure", "tokenShapeOk"):
        assert fn in names, fn
    text = cases_text()
    assert "access mode unknown" in ds.script_text()
    assert re.search(r"name: 'lockState', args: \[\{ enforce_access: null \}", text)


def test_the_token_header_is_built_in_one_place():
    code = ds.strip_comments(ds.script_text())
    assert code.count("X-Admin-Token") == 1
    body = ds.functions(ds.script_text())["authHeaders"]
    assert "X-Admin-Token" in body and "needsToken(" in body


def test_token_storage_key_and_no_local_storage_or_url_or_console():
    code = ds.strip_comments(ds.script_text())
    assert re.search(r"const TOKEN_KEY = 'cm\.token';", code)
    for m in re.finditer(r"sessionStorage\.(\w+)\(([^)]*)\)", code):
        assert m.group(2).startswith("TOKEN_KEY"), m.group(0)
    assert not re.search(r"localStorage\.\w+\(\s*TOKEN_KEY", code)
    assert "token=" not in code.lower()
    assert "console." not in code
    # what is written to the page: identifiers and template expressions only (literal text may say "token")
    exprs = "".join(chunk if kind == "code" else " ".join(re.findall(r"\$\{([^}]*)\}", chunk)) if chunk.startswith("`") else "''"
                    for kind, chunk in ds.scan(code))
    for m in re.finditer(r"(textContent|\.title|\.value|setStatus\(|toast\()\s*=?\s*([^;\n]*)", exprs):
        assert not re.search(r"storedToken|readToken|memToken|\btoken\b|authHeaders|tokenInput'\)\.value", m.group(2)), m.group(0)


def test_unlock_form_input():
    attrs = {a.get("id"): a for t, a in ds.html_parser().tags if a.get("id")}
    inp = attrs["tokenInput"]
    assert inp.get("type") == "password" and inp.get("autocomplete") == "off" and inp.get("aria-label")
    assert {"unlockBtn", "lockBtn", "lockText", "tokenForm", "tokenFile"} <= set(attrs)
    fns = ds.functions(ds.script_text())
    submit = ds.strip_comments(ds.section("INIT") + ds.section("VIEWS"))
    assert "$('tokenInput').value = ''" in submit
    assert "tokenShapeOk(" in submit and "saveToken(" in submit
    assert "'Unlocked'" in fns["applyLock"]


def test_auth_failure_handling():
    fns = ds.functions(ds.script_text())
    h = fns["handle401"]
    assert "afterAuthFailure(" in h and "clearToken()" in h
    pure = pure_block()
    for text in ("'token rejected'", "'unlock first'", "blocked by the hub", "Retry", "too many requests"):
        assert text.lower() in pure.lower(), text
    req = fns["request"]
    assert "handle401(" in req and "errorFromResponse(" in req
    assert re.search(r"startsWith\('/api/'\)", req)


def test_action_button_and_lock():
    fns = ds.functions(ds.script_text())
    ab = fns["actionButton"]
    assert "data" in ab and "action" in ab and "lockButton(" in ab
    lb = fns["lockButton"]
    assert "actionNeedsToken(" in lb and "'unlock first'" in lb and ".locked" in lb
    al = fns["applyLock"]
    assert "lockState(" in al


def test_ui_config_is_fetched_once_at_start_with_a_fallback():
    init = ds.strip_comments(ds.section("INIT"))
    assert init.count("loadUiConfig(") == 1
    body = ds.functions(ds.script_text())["loadUiConfig"]
    assert "'/api/ui-config'" in body and "UI_FALLBACK" in body and "tokenFile" in body
    pure = pure_block()
    m = re.search(r"const UI_FALLBACK = (\{.*?\});", pure, re.S)
    assert m and "enforce_access: null" in m.group(1) and "token_file: null" in m.group(1)
    for f in ("syslog-text", "ncsa", "w3c", "xferlog", "plain", "syslog-udp", "syslog-tcp", "telnet", "irc", "file"):
        assert f"'{f}'" in m.group(1)


def test_hash_is_read_only_for_routing_and_the_self_test():
    """The token never comes from the address: the hash is read only by parseRoute and the #selftest check."""
    code = ds.strip_comments(ds.script_text())
    reads = re.findall(r"[^\n]*\bhere\.hash\b[^\n]*", code)
    assert reads
    for line in reads:
        assert "parseRoute(here.hash)" in line or "here.hash === '#selftest'" in line or "here.hash = " in line, line
        assert "oken" not in line, line
    assert not re.search(r"\b(location|here)\.(search|href)\b", code)


# ---- task 4.3: the interim prompt is gone and the terminal goes through request() ------------------------

def test_interim_prompt_code_is_removed():
    code = ds.strip_comments(ds.script_text())
    for name in ("TOKEN_ROUTES", "askForToken", "chimericmesh.adminToken"):
        assert name not in code, name
    assert len(re.findall(r'id="tokenForm"', ds.load_page())) == 1  # one Unlock control
    assert len(re.findall(r"\bfetch\s*\(", code)) == 1  # only request() calls fetch


def test_terminal_posts_through_request_and_reports_401_for_approve_and_deny():
    fns = ds.functions(ds.script_text())
    run = fns["runCommand"]
    assert "request('POST', '/api/terminal'" in run
    assert re.search(r"status === 401", run)
    assert re.search(r"/\^!\(approve\|deny\)\\b/", run)
    assert "'unlock first'" in run and "termStatus" in run
    attrs = {a.get("id"): a for t, a in ds.html_parser().tags if a.get("id")}
    assert attrs["termStatus"].get("role") == "status"
