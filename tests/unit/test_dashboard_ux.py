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
    assert "The mesh is not answering. Check that the hub is running, then try again." in text


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
    assert "resolveIncident(i.id)" in code("renderIncidents")
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
    assert "The mesh refused that (status ${" in pure and "Try again or check the hub log." in pure
