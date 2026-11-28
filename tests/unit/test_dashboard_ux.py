"""Static checks of the usability batches (docs/ux-plan.md, findings UX-xx).
The page is never executed here: these read the CSS, the markup and the script source, like the other dashboard tests.
"""
from __future__ import annotations
import re

import dashboard_static as ds
from test_dashboard_core import case_names, cases_text, pure_block


def css() -> str:
    return ds.load_page().split("</style>")[0]


def media(query: str) -> str:
    """Body of the first `@media (query) { ... }` block (balanced braces)."""
    c = css()
    start = c.index(f"@media ({query})")
    i = c.index("{", start)
    depth, j = 0, i
    while True:
        depth += c[j] == "{"
        depth -= c[j] == "}"
        if depth == 0:
            return c[i + 1: j]
        j += 1


def rule(text: str, selector: str) -> str:
    m = re.search(r"(?m)(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert m, selector
    return m.group(1)


def fns() -> dict[str, str]:
    return ds.functions(ds.script_text())


def code(name: str) -> str:
    return ds.strip_comments(fns()[name])


def th_texts(label: str) -> list[str]:
    page = ds.load_page()
    a = page.index(f'aria-label="{label}"')
    seg = page[a: page.index("</thead>", a)]
    return re.findall(r'<th scope="col">([^<]+)</th>', seg)


def array_const(name: str) -> list[str]:
    m = re.search(rf"const {name} = \[([^\]]*)\];", ds.strip_comments(ds.script_text()))
    assert m, name
    return re.findall(r"'([^']+)'", m.group(1))


# ---- B1 mobile layout --------------------------------------------------------------------------------

def test_ux06_send_form_wraps_and_columns_can_shrink():
    """UX-06: the Seance log form forced #evPanel to 439 px on a 390 px phone."""
    c = css()
    assert "flex-wrap: wrap" in rule(c, "form.send, form.propose")
    assert re.search(r"form\.send input\[type=text\]\s*\{[^}]*flex: 1 1 12ch[^}]*min-width: 0", c)
    assert "min-width: 0" in rule(c, "#evPanel, #incPanel, #propPanel, #termPanel")


def test_ux11c_unlock_prompt_wraps():
    """UX-11 (c): Cancel was cut off at the right edge of the phone."""
    c = css()
    assert "flex-wrap: wrap" in rule(c, "form.unlock .prompt")
    assert re.search(r"form\.unlock \.prompt input\s*\{[^}]*flex: 1 1 14ch[^}]*min-width: 0", c)


def test_ux21_touch_targets_on_phones():
    m = media("max-width: 480px")
    assert "min-height: 40px" in rule(m, "button")
    assert "gap: 10px" in rule(m, ".actions")


def test_ux07_rule_and_audit_tables_stack_on_phones():
    m = media("max-width: 640px")
    assert "display: none" in rule(m, "table.rules thead")
    assert "display: block" in rule(m, "table.rules tr")
    assert "attr(data-label)" in rule(m, "table.rules td::before")
    td = rule(m, "table.rules td")
    assert "min-width: 0" in td and "overflow-wrap: break-word" in td
    # the table scrolls sideways only on wide screens, and only inside its wrapper
    assert ".table-wrap { overflow-x: auto; }" in css()


def test_ux07_cells_carry_a_label_that_matches_the_column_header():
    rules_cols = array_const("RULE_COLS")
    audit_cols = array_const("AUDIT_COLS")
    assert rules_cols == th_texts("Approval rules")
    assert audit_cols == th_texts("Audit trail")
    rr, ar = code("ruleRow"), code("auditRow")
    assert "setAttribute('data-label'" in rr and "RULE_COLS" in rr
    assert "setAttribute('data-label'" in ar and "AUDIT_COLS" in ar
    assert len(re.findall(r"\bcell\(", rr)) == len(rules_cols) - 1  # nine text cells plus the actions cell


def test_ux07_actions_cell_is_labelled_too():
    rr = code("ruleRow")
    assert re.search(r"const td = el\('td'\);\s*td\.setAttribute\('data-label', RULE_COLS\[", rr)


def test_ux06_stacked_grid_stretches_its_panels():
    """At 320 px the Overview terminal panel was shrink-wrapped to its min-content (357 px) by `align-items: start`."""
    m = media("max-width: 960px")
    assert "align-items: stretch" in rule(m, ".grid")


# ---- B2 approve path first ---------------------------------------------------------------------------

def page_ids() -> dict[str, dict]:
    return {a.get("id"): a for _, a in ds.html_parser().tags if a.get("id")}


def test_ux01_decide_strip_sits_between_stats_and_guide():
    page = ds.load_page()
    assert page.index('id="stats"') < page.index('id="decideStrip"') < page.index('id="guide"')
    attrs = page_ids()["decideStrip"]
    assert attrs.get("role") == "status" and "hidden" in attrs
    assert 'id="decideReview"' in page and 'id="decideText"' in page


def test_ux01_strip_is_filled_with_text_and_review_focuses_the_proposals():
    body = code("renderDecideStrip")
    assert "decideText(" in body and "textContent" in body and "has-pending" in body
    assert "innerHTML" not in body
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"\$\('decideReview'\)\.onclick = [^;]*scrollFocus\(\$\('propPanel'\)\)", script)
    assert "renderDecideStrip(" in code("renderProposals")
    assert "decideText" in ds.functions(pure_block())


def test_ux01_decide_text_has_cases():
    assert case_names().count("decideText") >= 4
    text = cases_text()
    assert "A fix is waiting for you: Add bounded backoff" in text
    assert "2 fixes are waiting for you. Newest: " in text


def test_ux01_pending_proposals_move_above_the_incidents():
    c = css()
    assert re.search(r"\.col\.left\.has-pending #propPanel \{ order: -1; \}", c)
    narrow = media("max-width: 960px")
    assert re.search(r"\.col\.left\.has-pending #propPanel\s*\{[^}]*order: 0", narrow)


def test_ux02_service_page_puts_proposals_before_the_live_log():
    page = ds.load_page()
    seg = page[page.index('id="view-service"'): page.index("<!-- /view-service -->")]
    assert re.findall(r'data-slot="([a-z-]+)"', seg) == ["proposals", "incidents", "live-log", "permissions"]
    assert seg.index('id="svcDetail"') < seg.index('data-slot="proposals"')
    assert "max-height: 260px" in rule(css(), "#liveBox.log")


def test_ux05_both_pages_have_a_visible_lock_note_with_an_unlock_button():
    ids = page_ids()
    assert "propNote" in ids and "svcPropNote" in ids
    body = code("renderLockNote")
    assert "openUnlock" in body and "el('span', '', text)" in body and "replaceChildren" in body
    upd = code("updatePropNote")
    assert "renderLockNote($('propNote')" in upd and "renderLockNote($('svcPropNote')" in upd
    assert "unlock first" in upd and "Withdraw works without it" in upd
    assert "updatePropNote(" in code("renderServiceProposals")


def test_ux05_approve_and_deny_point_at_the_note_and_withdraw_is_quiet():
    card = code("renderProposalCard")
    assert "setAttribute('aria-describedby', noteId)" in card
    assert re.search(r"actionButton\('proposal-withdraw'[^\n]*'ghost small quiet'", card)
    assert re.search(r"actionButton\('proposal-approve'[^\n]*'small'", card)
    assert "renderProposalCard(p, " in code("renderProposals") and "'propNote'" in code("renderProposals")
    assert "'svcPropNote'" in code("renderServiceProposals")
    assert "button.quiet" in css()
    # R2.4: still disabled, never aria-disabled
    assert "b.disabled = true" in code("lockButton") and "aria-disabled" not in ds.script_text()


def test_ux20_skip_link_and_main_target():
    page = ds.load_page()
    assert page.index('class="skip"') < page.index("<header>")
    assert re.search(r'<main id="main" tabindex="-1">', page)
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"\$\('skip'\)\.onclick = [^;]*preventDefault\(\)[^}]*\$\('main'\)\.focus\(\)", script)
    assert ".skip:focus" in css()
    assert 'class="pulse" aria-hidden="true"' in page


# ---- B3 feedback and errors --------------------------------------------------------------------------
def test_ux03_toast_is_a_polite_live_region_that_never_takes_focus():
    attrs = page_ids()["toast"]
    assert attrs.get("role") == "status" and attrs.get("aria-live") == "polite"
    body = code("toast")
    assert "textContent" in body and "focus" not in body and "innerHTML" not in body
    assert "position: fixed" in css().split("#toast {")[1].split("}")[0]


def test_ux03_action_toast_and_error_text_are_pure_and_have_cases():
    pure = ds.functions(pure_block())
    for name in ("actionToast", "friendlyError", "unlockOutcome"):
        assert name in pure, name
    names = case_names()
    assert names.count("actionToast") >= 4 and names.count("friendlyError") >= 2 and names.count("unlockOutcome") >= 3
    text = cases_text()
    assert 'Approved p4. The agent will apply it; this card turns "done" when it reports back.' in text
    assert "Denied p4. The agent will not run it." in text and "Withdrew p4. Nothing will run." in text
    assert "That token was not accepted. The ghost wants the exact line from the token file." in pure_block()
    assert "The mesh went quiet. Check that the hub is running, then try again." in text


def test_ux03_approve_deny_withdraw_toast_and_keep_the_card_message():
    for name, kinds in (("decideProposal", ("'approve'", "'deny'")), ("withdrawProposal", ("'withdraw'",))):
        body = code(name)
        assert "toast(actionToast(" in body and all(k in body for k in kinds), name
        assert "flashMessage(" in body, name
        assert "setStatus(" in body, name  # R2.3: the header text stays
    card = code("renderProposalCard")
    assert "STATE.flash" in card and "dataset.flash" in card
    flash = code("flashMessage")
    assert "STATE.flash.set(" in flash and "setTimeout(" in flash and "8000" in flash
    assert "flash: new Map()" in ds.strip_comments(ds.section("STATE"))


def test_ux17_resolve_and_storm_confirm_on_the_page():
    body = code("resolveIncident")
    assert "!resolve ${" in body and "via dashboard" in body
    assert "toast(actionToast(inc && inc.resolved ? 'resolve' : 'unconfirmed'" in body
    assert "resolveIncident(i.id)" in code("appendIncidentActions")
    st = code("storm")
    assert "toast(actionToast('storm'" in st
    assert "return true" in code("runCommand") and "return false" in code("runCommand")


def test_ux04_unlock_checks_the_token_with_a_privileged_read_before_saying_unlocked():
    wire = code("wireUnlock")
    assert "async (ev)" in wire
    assert "request('GET', '/api/discovery/dismissals')" in wire
    assert wire.index("request('GET', '/api/discovery/dismissals')") < wire.index("setStatus(kept ? out.text")
    assert "'Unlocked'" in pure_block()  # the only place the word is produced is unlockOutcome(200)
    assert "unlockOutcome(" in wire and "clearToken()" in wire
    assert "'Token saved" not in ds.script_text()
    # that route is on the privileged table, so request() sends the token with it and the hub checks it
    assert "['GET', '^/api/discovery/dismissals$']" in pure_block()


def test_ux04_a_rejected_token_toasts_and_reopens_the_unlock_form_with_a_message():
    h = code("handle401")
    assert "toast(TOKEN_REJECTED_TEXT)" in h and "openUnlock(TOKEN_REJECTED_TEXT)" in h
    assert h.index("clearToken()") < h.index("openUnlock(")
    assert "setStatus(out.text)" in h  # R2.3: "token rejected" stays in the status area
    op = code("openUnlock")
    assert re.search(r"function openUnlock\(note\)", ds.script_text()) and "note || TOKEN_PROMPT_TEXT" in op
    assert "$('unlockBtn').onclick = () => openUnlock();" in ds.script_text()  # the click event is not a note


def test_ux23_error_copy_is_friendly_but_the_pinned_strings_stay():
    assert "friendlyError(0)" in code("unreachable")
    assert "friendlyError(" in code("openPropose")
    pure = pure_block()
    for pinned in ("'token rejected'", "'unlock first'"):
        assert pinned in pure
    assert "The mesh turned that away (status ${" in pure and "Try again or check the hub log." in pure


# ---- B4 close the loop -------------------------------------------------------------------------------
def test_ux18_close_loop_helper_has_cases():
    assert "closeLoop" in ds.functions(pure_block()) and "fixLine" in ds.functions(pure_block())
    names = case_names()
    assert names.count("closeLoop") >= 4 and names.count("fixLine") >= 3
    text = cases_text()
    assert "done, tests passed" in text and "Tests failed. Deny it and ask the agent for another fix." in text
    assert "fix #p4 approved" in text


def test_ux18_done_card_shows_the_pill_and_a_lay_to_rest_button_using_the_shared_resolve():
    card = code("renderProposalCard")
    assert "closeLoop(p, " in card and "Lay incident to rest" in card
    assert "resolveIncident(p.incident_id)" in card
    assert "innerHTML" not in card
    assert "textContent" in ds.strip_comments(ds.script_text())
    assert ".meta.warnline" in css()


def test_ux18_incident_card_shows_the_linked_fix_and_rerenders_when_it_changes():
    inc = code("renderIncidents")
    assert "fixLine(" in inc and "fixline" in inc
    assert "renderIncidents(" in code("renderProposals")
    assert "i.resolved" in code("renderProposals")  # the proposals key follows the incident state


def test_ux09_incident_cards_name_their_service_with_a_button_not_a_link():
    inc = code("appendIncidentActions")
    assert "i.service_id" in inc and "`service: ${" in inc
    assert re.search(r"here\.hash = `#/services/\$\{encodeURIComponent\(i\.service_id\)\}`", inc)
    assert "href" not in inc


def test_ux09_service_cards_show_a_fix_waiting_pill_and_singular_incident_text():
    card = code("serviceCard")
    raw = ds.functions(ds.script_text())["serviceCard"]
    assert "pendingFor(STATE.navProp, s.id)" in card
    assert "'fix' : 'fixes'} waiting" in raw and "open incident${open === 1 ? '' : 's'}" in raw
    assert "pendingFor" in ds.functions(pure_block())
    assert case_names().count("pendingFor") >= 2


def test_ux19_nav_counters_are_set_with_text_and_extend_the_accessible_name():
    page = ds.load_page()
    nav = re.search(r'<nav aria-label="Views">(.*?)</nav>', page, re.S).group(1)
    assert nav.count('class="count"') == 2
    body = code("renderNav")
    assert "navBadges(" in body and "textContent" in body and "setAttribute('aria-label'" in body
    assert "innerHTML" not in body
    assert "navBadges" in ds.functions(pure_block()) and case_names().count("navBadges") >= 3
    assert "Services, 2 waiting" in cases_text()
    sched = ds.strip_comments(ds.section("SCHEDULER"))
    assert re.search(r"id: 'nav-counts', every: 5000, last: 0, view: '\*'", sched)


# ---- B5 service page and services list ---------------------------------------------------------------
def test_ux08_service_page_has_a_crumb_back_to_the_list_and_a_state_pill():
    page = ds.load_page()
    assert re.search(r'<a href="#/services" id="svcCrumb">All services</a>', page)
    assert page.index('id="svcCrumb"') < page.index('id="svcName"')
    assert 'id="svcPill"' in page
    body = code("renderServiceHeader")
    assert "svcPill" in body and "STATE_CLASS" in body and "stateLabel('service', s.state)" in body
    assert "innerHTML" not in body


def test_ux09a_service_page_incidents_share_the_overview_actions():
    shared = code("appendIncidentActions")
    for act in ("'explain'", "'code'", "'propose'", "'resolve'"):
        assert act in shared
    assert "!i.resolved" in shared and "resolveIncident(i.id)" in shared
    assert "appendIncidentActions(" in code("renderIncidents")
    on_service = code("renderIncidentsFor")
    assert "appendIncidentActions(" in on_service and "service: " not in on_service
    assert "i.service_id" in shared and "noService" in shared


def test_ux09a_service_page_keeps_a_propose_form_while_it_refreshes():
    body = code("loadServiceLists")
    assert "form.propose" in body
    assert "key !== d.incKey" in body


def test_ux10_service_meta_is_three_unbreakable_pieces_and_unassigned_is_explained():
    c = css()
    assert "white-space: nowrap" in rule(c, ".svc-card .facts-line span")
    card = code("serviceCard")
    assert "facts-line" in card and card.count("el('span'") >= 3
    assert "Events no service claims yet. Add a service or run discovery to claim them." in ds.functions(ds.script_text())["serviceCard"]
    assert "s.builtin" in card


def test_ux22_service_form_asks_for_the_name_first_and_suggests_the_id():
    body = code("renderServiceForm")
    assert body.index("'svcFName'") < body.index("'svcFId'")
    assert "slugify(" in body and "idEdited" in body
    assert "Which feeds belong to this service. Leave empty to match by host or app name only." in ds.functions(ds.script_text())["renderServiceForm"]
    assert "slugify" in ds.functions(pure_block())
    assert case_names().count("slugify") >= 1  # the self-test is capped at 200 lines (test_dashboard_core)


# ---- B6 rules, audit and discovery wording -----------------------------------------------------------
def test_ux15_actor_action_subject_helpers_are_pure_and_have_cases():
    pure = ds.functions(pure_block())
    for name in ("actorText", "actionText", "subjectText"):
        assert name in pure
        assert case_names().count(name) >= 1
    body = ds.strip_comments(pure["actorText"])
    assert "'API'" in body and "'browser'" in body and "rest:" in body and "client:" in body


def test_ux15_audit_rows_show_the_helpers_and_keep_the_raw_value_as_a_tooltip():
    body = code("auditRow")
    for helper in ("actorText(r.actor)", "actionText(r.action)", "subjectText(r.subject)"):
        assert helper in body
    assert "innerHTML" not in body
    assert ".title" in body  # the raw actor stays reachable
    assert 'class="rules audit"' in ds.load_page()
    assert "white-space: nowrap" in rule(css(), "table.audit td:nth-child(1), table.audit td:nth-child(3)")
    assert "white-space: normal" in media("max-width: 640px")


def test_ux15_stamps_use_a_24_hour_clock():
    body = code("fmtStamp")
    assert "hour12: false" in body and "toLocaleString([]" in body


def test_ux15_rules_page_has_a_legend_and_the_preview_asks_for_a_signature():
    page = ds.load_page()
    assert 'id="rulesLegend"' in page and "Paused rules can be resumed. Revoked rules are gone." in page
    assert page.index('id="rulesLegend"') < page.index('id="rulesMsg"')
    assert "Type the signature to match." in ds.functions(ds.script_text())["renderRuleForm"]


def test_ux16_event_times_are_24_hour_and_do_not_wrap():
    body = code("renderEvents")
    assert "fmtTime(e.ts)" in body and "toLocaleTimeString" not in body
    assert "white-space: nowrap" in rule(css(), ".row .t")


def test_ux14_empty_scan_explains_and_offers_a_manual_add():
    body = code("renderDiscovery")
    assert "No apps found. Nothing visible is listening here. Add your service by hand." in ds.functions(ds.script_text())["renderDiscovery"]
    assert "discEmptySlot" in body and "renderServiceForm(null)" in body and "'#/services'" in body
    assert "no apps found" not in body
    assert "innerHTML" not in body


def test_ux14_locked_discovery_says_why_the_scan_button_is_dimmed():
    page = ds.load_page()
    assert 'id="discLockHint"' in page
    assert page.index('id="discExplainer"') < page.index('id="discLockHint"') < page.index('id="discScanSlot"')
    assert "Scanning needs the admin token. Unlock first." in ds.script_text()
    assert "updateDiscLock" in code("applyLock") and "updateDiscLock" in code("renderDiscovery")

def test_ux15_rule_pause_reason_shows_the_actor_in_plain_words():
    assert "String(r.status_reason).replace(" in code("ruleRow") and "actorText" in code("ruleRow")


# ---- B7 first run and copy ---------------------------------------------------------------------------
import json
import shutil
import subprocess
import pytest
def run_pure(expr: str):
    """Evaluate a JS expression with the PURE block in scope (node), return the JSON result."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = "(function(){" + pure_block() + "\nreturn JSON.stringify(" + expr + ");})()"
    out = subprocess.run([node, "-e", f"process.stdout.write({src})"], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)
# Coverage removed from the in-page self-test in B6 (200-line cap), restored here by running the real code.
def test_pure_needs_token_matches_the_privileged_table():
    r = run_pure("[needsToken('POST','/api/services'), needsToken('GET','/api/discovery/dismissals?x=1'),"
                 " needsToken('GET','/api/incidents'), needsToken('POST','/api/proposals/p1/decide'),"
                 " needsToken('POST','/api/demo/storm')]")
    assert r == [True, True, False, True, False]
def test_pure_state_label_known_unknown_and_inherited_keys():
    assert run_pure("[stateLabel('service','never_seen'), stateLabel('proposal','done'), stateLabel('service','bogus'),"
                    " stateLabel('nope','active'), stateLabel('service','constructor')]") == ["never seen", "done", "unknown", "unknown", "unknown"]
def test_pure_next_backoff_doubles_to_a_cap_and_ignores_bad_input():
    assert run_pure("[0,1,2,3,4,5,6,99,-1,2.5,'x',null].map(nextBackoff)") == [1, 2, 4, 8, 16, 30, 30, 30, 1, 1, 1, 1]
def test_ux13_storm_button_is_demoted_once_an_incident_exists():
    assert run_pure("[stormLabel(0), stormLabel(1), stormLabel(undefined), stormLabel(3)]") == [
        "Summon a failure storm", "Another storm", "Summon a failure storm", "Another storm"]
def test_ux13_header_storm_is_never_the_filled_button_and_follows_the_incident_list():
    assert re.search(r'<button id="storm" class="ghost small"', ds.load_page())
    assert "stormLabel(" in code("renderIncidents") and "$('storm')" in code("renderIncidents")
def test_ux13_terminal_starts_with_a_hint_that_the_first_message_removes():
    page = ds.load_page()
    assert 'id="termHint"' in page and "ChimericBot is listening" in page and "Try !help." in page
    assert page.index('id="transcript"') < page.index('id="termHint"') < page.index('id="newMsgs"')
    assert "termHint" in code("appendMessages") and "tlog.replaceChildren()" in ds.script_text()
def test_ux12_guide_lock_hint_is_pure_and_set_with_text_content():
    assert run_pure("[guideLockText({complete:false}, true), guideLockText({complete:true}, true), guideLockText({complete:false}, false), guideLockText(null, true)]") == [
        "Step 5 needs the admin token. Unlock first.", "", "", ""]
    page = ds.load_page()
    assert 'id="guideLockHint"' in page and page.index('id="guideSteps"') < page.index('id="guideLockHint"')
    body = code("updateGuideLock")
    assert "guideLockText(" in body and "textContent" in body and "innerHTML" not in body
    assert "updateGuideLock" in code("applyLock") and "updateGuideLock" in code("loadWorkflow")
def test_ux12_guide_grid_fits_six_steps_in_two_or_three_columns():
    assert "minmax(180px, 1fr)" in rule(css(), ".steps")
def test_ux24_permissions_summary_says_it_is_optional_and_copy_comes_first():
    page = ds.load_page()
    assert "<summary>Kiro permissions (optional, for hands-off edits)</summary>" in page
    assert page.index('id="permCopy"') < page.index('id="permText"')
def test_b7_empty_and_error_copy_names_the_problem_and_the_next_step():
    s = ds.script_text()
    for text in ("Nobody has haunted a fix into being", "The mesh went quiet. Check that the hub is running, then try again.",
                 "The mesh turned that away (status ${status}). Try again or check the hub log."):
        assert text in s, text
    page = ds.load_page()
    assert "This service is gone (or never existed)." in page and 'id="svcBack"' in page
    assert "Locked: view only" in s and ">Unlock to approve</button>" in page
    assert "Open that file, copy its single line, paste it here." in s
    # security-relevant wording is unchanged
    for pinned in ("'token rejected'", "'unlock first'"):
        assert pinned in s
    assert "That token was not accepted." in s