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


# ---- 5.3 permissions panel -----------------------------------------------------------------------------

def test_permissions_panel_markup():
    seg = segment('data-slot="permissions"', "</details>")
    assert "<details" in seg and "<summary>Kiro permissions</summary>" in seg
    assert "ChimericMesh never writes this file; paste it yourself." in seg
    assert re.search(r'<pre id="permText"', seg) and 'id="permCopy"' in seg


def test_permissions_text_is_fetched_on_first_open_only_and_shown_as_text():
    panel = code("renderPermissionsPanel")
    load = code("loadPermissions")
    assert "'toggle'" in panel or "ontoggle" in panel
    assert "/permissions-snippet`" in load and "'text'" in load
    assert "textContent" in load
    assert "e.status === 404" in load and "'not available yet'" in load and "toast(" not in load
    init = ds.strip_comments(ds.section("INIT"))
    assert "loadPermissions(" not in init and "permissions-snippet" not in init


def test_copy_uses_the_clipboard_with_a_select_fallback():
    copy = code("copyPermissions")
    assert "navigator.clipboard.writeText(" in copy
    assert "selectNodeContents(" in copy and "Ctrl+C" in copy


# ---- review-1 fixes -------------------------------------------------------------------------------------

def test_save_cannot_run_twice_at_once():
    """review-1 M1: a double click must not send the create or a log-source POST twice."""
    save = code("saveService")
    assert re.search(r"if \(!st \|\| st\.busy\) return;", save)
    assert "st.busy = true;" in save and re.search(r"finally \{\s*st\.busy = false;", save)
    assert save.index("st.busy = true;") < save.index("request(")

# ---- 6.1 discovery: scan start and polling ---------------------------------------------------------------
def test_discovery_explainer_is_shown_before_the_scan_button():
    script = ds.strip_comments(ds.script_text())
    m = re.search(r"const DISCOVERY_EXPLAINER = '([^']+)';", script)
    assert m, "DISCOVERY_EXPLAINER constant"
    text = m.group(1)
    assert "processes" in text and "listening ports" in text
    assert "never reads log contents" in text and "nothing leaves this machine" in text
    seg = segment('id="view-discovery"', "<pre id=\"selftest\"")
    assert seg.index('id="discExplainer"') < seg.index('id="discScanSlot"')
    render = code("renderDiscovery")
    assert "DISCOVERY_EXPLAINER" in render and "textContent" in render
def test_scan_is_started_only_by_a_click_handler():
    script = ds.strip_comments(ds.script_text())
    assert script.count("'/api/discovery/scan'") == 1
    assert "'/api/discovery/scan'" in code("startScan")
    # the only call-looking text is the definition; every other mention is a reference passed as a handler
    assert len(re.findall(r"startScan\s*\(", script)) == 1, "only the definition looks like a call"
    refs = [m.start() for m in re.finditer(r"\bstartScan\b", script)]
    assert len(refs) == 2, "the definition and one click handler"
    assert re.search(r"actionButton\('scan-start', 'Discover local apps', startScan\b", script)
    for name in ("pollScan", "adoptScan"):
        assert "startScan" not in code(name), name
    assert "startScan" not in ds.strip_comments(ds.section("INIT"))
    sched = ds.strip_comments(ds.section("SCHEDULER"))
    assert "startScan" not in sched
def test_scan_poll_is_a_one_second_task_that_only_works_while_a_scan_runs():
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["scan-poll"] == {"id": "scan-poll", "every": "1000", "view": "discovery"}
    sched = ds.strip_comments(ds.section("SCHEDULER"))
    assert re.search(r"id: 'scan-poll'.*run: \(\) => [^\n]*scanRunning\(\)", sched)
    assert re.search(r"function scanRunning\(\)", ds.script_text())
    poll = code("pollScan")
    assert "`/api/discovery/scans/${encodeURIComponent(" in poll
    assert "sc.state !== 'running'" in poll
def test_scan_texts_and_status_codes():
    script = ds.strip_comments(ds.script_text())
    assert script.count("some processes were not visible without admin rights") == 1
    assert "sc.limited" in code("renderDiscovery")
    start = code("startScan")
    assert "e.status === 409" in start and "scan_id" in start and "adoptScan(" in start
    assert "errorText(" in start  # 503 and other statuses show the server text
    poll = code("pollScan")
    assert "SCAN_GIVE_UP_MS" in poll and "scan is taking too long" in script
    assert re.search(r"SCAN_GIVE_UP_MS = 45000", script)
    assert "e.status === 404" in poll and "scan expired, run it again" in script
def test_discovery_view_does_not_scan_on_load_or_on_entry():
    enter = re.search(r"ENTER\.discovery = [^\n]*", ds.strip_comments(ds.script_text()))
    assert enter and "startScan" not in enter.group(0) and "request(" not in enter.group(0)
    assert "/api/discovery" not in ds.strip_comments(ds.section("INIT"))

# ---- 6.2 discovery: suggestion review, accept and dismiss -------------------------------------------------
def test_suggestion_card_shows_the_evidence_and_the_candidate_checklist():
    card = code("renderSuggestionCard")
    for piece in ("s.name", "s.confidence", "evidence.process", "evidence.pid", "evidence.process_count",
                  "evidence.ports", "candidate_log_paths", "'exists'", "'not found'", "'not readable'",
                  "Auto-approved fixes may only change files in this folder. You can also set it later."):
        assert piece in card or piece in ds.strip_comments(ds.script_text()), piece
    assert "field(" in card and "uiConfig.formats" in card
    for label in ("'Id'", "'Name'", "'Code folder (optional)'", "'Use'", "'Path'", "'Format'"):
        assert label in card, label
    assert "actionButton('suggestion-accept'" in card and "actionButton('suggestion-dismiss'" in card
def test_checked_by_default_only_when_the_file_exists_and_is_not_watched():
    card = code("renderSuggestionCard")
    assert re.search(r"cb\.checked = Boolean\(c\.exists && !c\.watched_by\)", card)
    assert re.search(r"if \(c\.watched_by\)", card)
    assert "already watched by " in card
    assert re.search(r"cb\.disabled = true", card)
def test_accepted_suggestions_show_the_service_instead_of_the_form():
    card = code("renderSuggestionCard")
    assert "s.state === 'accepted'" in card
    assert "here.hash = `#/services/${encodeURIComponent(" in card
    assert card.index("s.state === 'accepted'") < card.index("actionButton('suggestion-accept'")
def test_accept_sends_only_checked_paths_through_buildacceptbody():
    acc = code("acceptSuggestion")
    assert "buildAcceptBody(" in acc
    assert "`/api/discovery/suggestions/${encodeURIComponent(" in acc and "/accept`" in acc
    assert "request('POST'" in acc
    assert "e.status === 409" in acc and "refreshScan(" in acc
    assert "errorText(" in acc and "textContent" in acc
    assert "renderSuggestionCard(" in acc and "replaceWith(" in acc  # 201: the card becomes the service link
    assert "service" in acc
    # the body is built from the form only, never from the suggestion's unchecked or watched rows
    assert "log_paths" not in acc
def test_dismiss_calls_the_endpoint_and_hides_the_card():
    dis = code("dismissSuggestion")
    assert "/dismiss`" in dis and "request('POST'" in dis
    assert "card.remove()" in dis
    clear = code("clearDismissals")
    assert "request('DELETE', '/api/discovery/dismissals')" in clear
    assert "actionButton('dismissals-clear', 'Show dismissed apps again'" in code("renderDiscovery")
def test_suggestions_are_not_rebuilt_while_the_user_types():
    render = code("renderDiscovery")
    assert "renderSuggestions(" in render
    body = code("renderSuggestions")
    assert "shownKey" in body and "JSON.stringify(" in body
    assert "sc.state === 'running'" in body or "sc.state !== 'done'" in body
def test_accept_body_cases_cover_the_documented_inputs():
    names = case_names()
    assert names.count("buildAcceptBody") >= 4
    text = cases_text()
    assert "log_paths: []" in text  # nothing checked, and a watched_by row that is never sent
    assert re.search(r"expect: \{ id: 'a', name: 'A', log_paths: \[\{ path: '/x', format: 'ncsa' \}, \{ path: '/y', format: 'w3c' \}\] \}", text)

# ---- 7.1 service detail: live log ------------------------------------------------------------------------
def live_markup() -> str:
    return segment('data-slot="live-log"', 'data-slot="incidents"')
def test_live_log_markup_has_labelled_controls():
    seg = live_markup()
    for ident in ("livePause", "liveAuto", "liveSev", "liveText", "liveStatus", "liveBox"):
        assert f'id="{ident}"' in seg, ident
    for ident in ("liveAuto", "liveSev", "liveText"):
        assert f'<label for="{ident}">' in seg, ident
    assert re.search(r'<button[^>]*id="livePause"[^>]*>Pause</button>', seg)
    assert 'role="log"' in seg and 'id="liveStatus" role="status"' in seg
def test_one_event_source_built_from_a_relative_stream_url():
    script = ds.strip_comments(ds.script_text())
    assert script.count("new EventSource(") == 1
    assert "new EventSource(streamUrl(" in code("openLive")
    url = code("streamUrl")
    assert "`/api/services/${encodeURIComponent(id)}/stream" in url
    assert "?tail=100" in url and "?after=${" in url
    assert "http" not in url and "/events" not in url
    assert not re.search(r"/api/services/\$\{[^}]*\}/events", script)  # H3: no REST backlog call
def test_rows_are_built_with_el_and_capped():
    add = code("addLiveRow")
    assert "el('div', 'row live'" in add
    assert "capText(" in add and "LOG_MSG_SHORT" in add and "LOG_MSG_LONG" in add
    assert "'show more'" in add
    assert "while (box.childElementCount > 500) box.firstElementChild.remove();" in add
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"const LOG_MSG_SHORT = 500, LOG_MSG_LONG = 4096, LOG_QUEUE_MAX = 500;", script)
    assert re.search(r"\.row\.live \.m \{[^}]*text-overflow: ellipsis", ds.load_page())
    assert "ev.severity_name || sevName(ev.severity)" in add
    assert "rowMatches(" in add
def test_live_controls_are_wired():
    wire = code("wireLive")
    for ident in ("livePause", "liveAuto", "liveSev", "liveText"):
        assert f"$('{ident}')" in wire, ident
    assert "rowMatches(" in code("applyLiveFilter")
    assert "LOG_QUEUE_MAX" in code("onLiveMessage")
    assert "toggleLivePause" in wire
    flush = code("toggleLivePause")
    assert "L.queue" in flush and "addLiveRow(" in flush
def test_rowmatches_has_cases():
    assert case_names().count("rowMatches") >= 5
    assert "rowMatches" in ds.functions(pure_block())
def test_closed_and_error_handlers_close_the_source_first():
    for name in ("onLiveClosed", "onLiveError"):
        body = code(name)
        assert ".close()" in body, name
        for later in ("scheduleLive(", "openLive("):
            if later in body:
                assert body.index(".close()") < body.index(later), name
    assert "'service_deleted'" in code("onLiveClosed") and "'max_age'" in code("onLiveClosed")
    closed = code("onLiveClosed")
    assert closed.index("'max_age'") < closed.index("openLive(")  # max_age reopens at once
def test_reconnect_backs_off_and_resumes_from_the_last_id():
    sched = code("scheduleLive")
    assert "nextBackoff(L.fails)" in sched and "setTimeout(" in sched
    assert "streamUrl(L.id, L.lastId)" in code("openLive")
    msg = code("onLiveMessage")
    assert "isNewer(ev.id, L.lastId)" in msg and "JSON.parse(" in msg and "try" in msg
    assert "L.fails = 0" in msg
    assert "too many live logs open (limit 20)" in code("onLiveError")
def test_dropped_inserts_a_system_row():
    dropped = code("onLiveDropped")
    assert "events were skipped" in dropped and "addSystemRow(" in dropped
    assert "addEventListener('dropped'" in code("openLive") and "addEventListener('closed'" in code("openLive")
def test_leaving_the_view_or_hiding_the_tab_closes_the_source():
    vis = code("liveVisibility")
    assert "'hidden'" in vis and "closeLive(" in vis and "openLive(" in vis
    assert "visibilitychange" in ds.strip_comments(ds.section("INIT")) and "liveVisibility" in ds.strip_comments(ds.section("INIT"))
    show = code("showRoute")
    assert "closeLive(" in show
    assert "startLive(" in code("openServiceDetail")

# ---- 7.2 proposal card, incidents and proposals per service ----------------------------------------------
def test_detail_loads_incidents_and_proposals_from_the_hub_filtered():
    load = code("loadServiceLists")
    assert "`/api/incidents?service_id=${id}&limit=100`" in load
    assert "`/api/proposals?service_id=${id}`" in load
    assert "encodeURIComponent(d.id)" in load
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["service-lists"] == {"id": "service-lists", "every": "5000", "view": "service"}
    assert "loadServiceLists()" in code("openServiceDetail")
    for name in ("loadServiceLists", "renderServiceProposals", "renderIncidentsFor"):
        assert ".filter(" not in code(name), f"{name} must not filter by service in JavaScript"
def test_proposals_are_shown_newest_first_with_one_renderer():
    body = code("renderServiceProposals")
    assert ".sort(" in body and "b.created" in body and "renderProposalCard(" in body
    assert "renderProposalCard(" in code("renderProposals")
    script = ds.strip_comments(ds.script_text())
    assert script.count("el('div', 'prop')") == 1 and "el('div', 'prop')" in code("renderProposalCard")
def test_proposal_card_fields_and_badge():
    card = code("renderProposalCard")
    for piece in ("stateLabel('proposal'", "p.action", "p.files", "slice(0, 5)", "more", "p.verification",
                  "approvalText(p)", "'AUTO-APPROVED'", "p.approval"):
        assert piece in card, piece
    assert re.search(r"p\.approval && p\.approval\.by === 'rule'", card)
    assert "p.decided_by" in card and "p.result_note" in card
def test_pending_cards_have_locked_approve_deny_and_a_token_free_withdraw():
    card = code("renderProposalCard")
    pending = card[card.index("p.status === 'pending'"):]
    for piece in ("actionButton('proposal-approve'", "actionButton('proposal-deny'", "actionButton('proposal-withdraw'"):
        assert piece in pending, piece
    assert "actionButton('proposal-approve'" not in card[: card.index("p.status === 'pending'")]
    dec = code("decideProposal")
    assert "request('POST', `/api/proposals/${encodeURIComponent(p.id)}/decide`, { approve, by: 'dashboard' })" in dec
    assert "errorText(" in dec and "textContent" in dec
    wd = code("withdrawProposal")
    assert "/withdraw`" in wd and "request('POST'" in wd and "{ approve" not in wd
def test_the_terminal_is_no_longer_used_to_approve_from_the_proposal_panel():
    script = ds.strip_comments(ds.script_text())
    assert not re.search(r"runCommand\(`!(approve|deny)", script)
    assert "!approve" not in code("renderProposals") and "!deny" not in code("renderProposals")
    prop = code("renderProposals")
    assert "CHIMERICMESH_OPERATORS" not in prop and "operator" not in prop
    note = code("updatePropNote")
    assert "propNote" in note and "unlock first" in note and "STATE.lock" in note
    assert "updatePropNote(" in code("applyLock")
def test_approval_text_has_cases_for_every_shape():
    assert case_names().count("approvalText") >= 8
    text = cases_text()
    for piece in ("approved by alice", "auto-approved by rule r1 (rule active)", "(rule suspended)", "(rule revoked)",
                  "(rule expired)", "(rule status unknown)"):
        assert piece in text, piece
    assert "approvalText" in ds.functions(pure_block())

# ---- 7.3 always approve fixes like this -------------------------------------------------------------------
def test_the_rule_button_shows_only_when_can_make_rule():
    card = code("renderProposalCard")
    assert re.search(r"if \(canMakeRule\(p\)\)", card)
    assert "'Always approve fixes like this'" in card and "openRuleFromProposal(" in card
    assert "canMakeRule" in ds.functions(pure_block())
    assert case_names().count("canMakeRule") >= 6
def test_opening_the_form_reads_the_defaults_and_creates_nothing():
    script = ds.strip_comments(ds.script_text())
    opn = code("openRuleFromProposal")
    assert "request('GET', `/api/proposals/${encodeURIComponent(p.id)}/rule-defaults`)" in opn
    assert "request('POST'" not in opn
    # the only POST to .../rule is in the confirm handler
    posts = re.findall(r"request\('POST', `/api/proposals/\$\{[^}]*\}/rule`", script)
    assert len(posts) == 1 and "request('POST', `/api/proposals/${encodeURIComponent(p.id)}/rule`" in code("confirmRuleFromProposal")
    assert "actionButton('rule-from-proposal'" in opn
    assert "ensureApprovalStatus(" in opn
def test_status_is_fetched_once_for_the_bounds_when_empty():
    body = code("ensureApprovalStatus")
    assert "if (STATE.approvalStatus) return STATE.approvalStatus;" in body
    assert "request('GET', '/api/approvals/status')" in body and "STATE.approvalStatus =" in body
def test_form_inputs_take_their_limits_from_the_bounds():
    form = code("renderRuleForm")
    assert "boundOf(" in form and "STATE.approvalStatus" in form and "RULE_FALLBACK" in form
    for key in ("most_urgent_severity", "max_per_hour", "max_per_incident", "max_files", "max_lines", "expires_days"):
        assert key in form or key in ds.strip_comments(ds.script_text()), key
    assert "i.min = String(b.min)" in code("numberInput") and "i.max = String(b.max)" in code("numberInput")
    assert "sevFloorText(" in form
    assert "Date.now" not in form and "Date.now" not in code("openRuleFromProposal") and "Date.now" not in code("confirmRuleFromProposal")
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"most_urgent_severity: \{ min: 3, max: 4, default: 3 \}", script)
    assert re.search(r"max_per_hour: \{ min: 1, max: 10, default: 3 \}", script)
    assert re.search(r"max_per_incident: \{ min: 1, max: 3, default: 1 \}", script)
    assert re.search(r"max_files: \{ min: 1, max: 20, default: 5 \}", script)
    assert re.search(r"max_lines: \{ min: 1, max: 500, default: 100 \}", script)
def test_confirm_sends_only_changed_fields_and_uses_the_server_expiry():
    conf = code("confirmRuleFromProposal")
    assert "ruleOverrides(defaults" in conf and "errorText(" in conf and "textContent" in conf
    assert "rf.form.remove()" in conf
    assert "loadRules" in conf
    over = ds.functions(pure_block())["ruleOverrides"]
    assert "expiryFromDays(defaults.expires_at" in over and "Date" not in over
    for fn in ("boundOf", "clampInt", "sevFloorText", "expiryFromDays", "ruleOverrides"):
        assert fn in ds.functions(pure_block()), fn
def test_pure_rule_helpers_have_the_documented_cases():
    names = case_names()
    assert names.count("expiryFromDays") >= 7 and names.count("clampInt") >= 5
    assert names.count("boundOf") >= 3 and names.count("sevFloorText") >= 3 and names.count("ruleOverrides") >= 4
    text = cases_text()
    assert "name: 'expiryFromDays', args: [1000000, NaN], expect: null" in text
    assert "name: 'expiryFromDays', args: [1000000, 2.5], expect: null" in text

# ---- 7.4 terminal, guide and identity line keep working ---------------------------------------------------
GOLDEN_FUNCTIONS = ["renderGuide", "doIt", "runCommand", "fetchChat", "complete", "renderIdentity", "renderIncidents",
                    "renderEvents", "renderStats"]
def test_the_existing_overview_functions_and_markup_are_still_there():
    names = set(fns())
    assert set(GOLDEN_FUNCTIONS) <= names, sorted(set(GOLDEN_FUNCTIONS) - names)
    script = ds.strip_comments(ds.script_text())
    assert "api('/api/workflow')" in code("loadWorkflow")
    assert "'/api/demo/storm'" in script and "'/api/ingest'" in script
    assert "`/api/chat?after=${state.chatCursor}&limit=200`" in code("fetchChat")
    ids = attrs_by_id()
    for ident in ("storm", "sendForm", "termForm", "chips", "guide", "guideSteps", "termInput", "transcript", "identity"):
        assert ident in ids, ident
    chips = re.findall(r'data-cmd="(![a-z]+)"', ds.load_page())
    assert chips == ["!help", "!status", "!incidents", "!proposals"]
    assert "history" in code("runCommand") and "state.history" in script
    assert ds.load_page().count('id="guideSteps"') == 1
def test_the_guide_code_is_byte_identical_to_the_baseline():
    for name, expected in ds.BASELINE_HASHES.items():
        assert ds.function_hash(name) == expected, name
def test_identity_line_shows_the_lock_state_not_the_operator_flag():
    ident = code("renderIdentity")
    assert "operator" not in ident
    assert "'unlocked'" in ident and "'locked (unlock to approve)'" in ident and "STATE.lock" in ident
    assert "renderIdentity()" in code("applyLock")
def test_terminal_401_for_approve_and_deny_says_unlock_first():
    run = code("runCommand")
    assert "e.status === 401 && /^!(approve|deny)\\b/.test(text)" in run and "'unlock first'" in run
    assert "request('POST', '/api/terminal'" in run

# ---- 8.1 rules table, reduce actions, paused banner ------------------------------------------------------
def rules_markup() -> str:
    return segment('id="view-rules"', "<pre id=\"selftest\"")
def test_rules_view_lists_rules_every_five_seconds():
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["rules-list"] == {"id": "rules-list", "every": "5000", "view": "rules"}
    assert "request('GET', '/api/rules')" in code("loadRules")
    assert "loadRules()" in ds.strip_comments(ds.script_text()) and "typeof loadRules" not in ds.strip_comments(ds.script_text())
def test_approval_status_task_runs_on_every_view_and_writes_the_state():
    rows = {r["id"]: r for r in sched_rows()}
    assert rows["approvals-status"] == {"id": "approvals-status", "every": "5000", "view": "*"}
    body = code("loadApprovalStatus")
    assert "request('GET', '/api/approvals/status')" in body and "STATE.approvalStatus =" in body
    assert "renderPausedBanner(" in body
    text = cases_text()
    assert re.search(r"name: 'dueTasks', args: \[\[\{ id: 'b', every: 5000, last: 0, view: '\*' \}\], 5000, 'services', true\], expect: \['b'\]", text)
    assert re.search(r"name: 'dueTasks', args: \[\[\{ id: 'b', every: 5000, last: 0, view: '\*' \}\], 5000, 'overview', true\]", text)
def test_rules_table_has_the_columns_of_the_requirement():
    seg = rules_markup()
    for head in ("Id", "Service", "Matches", "Severity floor", "Expires", "Limits", "Uses (hour / total)", "Status",
                 "Source proposal"):
        assert f'<th scope="col">{head}</th>' in seg, head
    assert seg.count("overflow-x") == 0 and 'class="table-wrap"' in seg
    row = code("ruleRow")
    for piece in ("r.match_type", "r.match_value", "sevFloorText(r.most_urgent_severity)", "countdown(r.expires_at",
                  "r.max_per_hour", "r.max_per_incident", "r.max_files", "r.max_lines", "r.uses_last_hour", "r.use_count",
                  "stateLabel('rule', r.status)", "r.source_proposal_id"):
        assert piece in row, piece
    assert re.search(r"r\.status === 'suspended' && r\.status_reason", row)
def test_pause_and_revoke_are_token_free_and_confirm_once():
    row = code("ruleRow")
    assert re.search(r"r\.status === 'active'[^\n]*actionButton\('rule-pause'", row)
    assert re.search(r"\(r\.status === 'active' \|\| r\.status === 'suspended'\)[^\n]*actionButton\('rule-revoke'", row)
    for name, path in (("pauseRule", "pause"), ("revokeRule", "revoke")):
        body = code(name)
        assert body.count("window.confirm(") == 1, name
        assert f"/api/rules/${{encodeURIComponent(r.id)}}/{path}`" in body, name
        assert body.index("window.confirm(") < body.index("reduceCall(")
    pa = code("pauseAll")
    assert pa.count("window.confirm(") == 1 and "'/api/rules/pause'" in pa
    red = code("reduceCall")
    assert "request('POST', path)" in red and "e.authText" in red  # 429 shows Retry-After through authText
    assert "X-Admin-Token" not in red
def test_pause_all_is_always_rendered_and_resume_needs_the_token():
    rules = code("renderRules")
    assert "actionButton('rules-pause-all', 'Pause all'" in rules
    assert "actionButton('rules-resume'" in code("renderPausedBanner")
    assert "'/api/rules/resume'" in code("resumeRules")
def test_paused_banner_text_actor_and_time():
    banner = code("renderPausedBanner")
    assert "Auto-approval is paused" in banner and "paused_by" in banner and "paused_at" in banner
    assert "!s || !s.paused" in banner and "b.hidden = true" in banner
    assert 'id="pausedBanner"' in ds.load_page()
    assert "loadApprovalStatus()" in ds.strip_comments(ds.section("INIT"))
