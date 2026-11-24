"""Accessibility and layout checks of the page (dashboard task 9.1; R8.1 to R8.3).
The page is never executed here. These are structural checks on the source: they catch a control that has no
name, a label that is missing, a lost focus ring, a status that is only a colour. Keyboard use and the 360 px
layout are also Manual checks (task 10.3).
"""
from __future__ import annotations
import re
from html.parser import HTMLParser
import dashboard_static as ds
from test_dashboard_core import cases_text, pure_block
class _Page(HTMLParser):
    """Collects, for the static HTML, each button's text and each control's labelling."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.buttons: list[tuple[dict, str]] = []
        self.controls: list[dict] = []
        self.label_for: set[str] = set()
        self.wrappers: list[str] = []
        self._btn: list | None = None
        self._label_depth = 0
        self.positive_tabindex: list[str] = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "button":
            self._btn = [a, ""]
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.controls.append(a)
        if a.get("tabindex") not in (None, "-1", "0"):
            self.positive_tabindex.append(str(a.get("tabindex")))
        if tag == "div" and "table-wrap" in (a.get("class") or ""):
            self.wrappers.append("table-wrap")
    def handle_data(self, data):
        if self._btn is not None:
            self._btn[1] += data
    def handle_endtag(self, tag):
        if tag == "button" and self._btn is not None:
            self.buttons.append((self._btn[0], self._btn[1]))
            self._btn = None
def page() -> _Page:
    p = _Page()
    p.feed(ds.load_page())
    p.close()
    return p
def test_every_static_button_has_a_name():
    p = page()
    assert p.buttons, "no buttons found"
    for attrs, text in p.buttons:
        assert text.strip() or attrs.get("aria-label"), attrs
def test_every_static_form_control_has_a_label_or_an_aria_label():
    p = page()
    assert p.controls
    for a in p.controls:
        assert a.get("aria-label") or (a.get("id") and a["id"] in p.label_for), a
def test_every_script_created_button_has_text():
    """el('button', cls, text): a call with fewer than three arguments creates a nameless button."""
    script = ds.strip_comments(ds.script_text())
    for m in re.finditer(r"el\('button'", script):
        depth, i, commas = 1, m.end(), 0
        while depth and i < len(script):
            c = script[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 1:
                commas += 1
            i += 1
        assert commas >= 2, script[m.start(): m.start() + 80]
def test_script_created_inputs_are_labelled():
    """Heuristic: a function that makes an input, select or textarea also labels it (field(), htmlFor or aria-label).
    textInput and numberInput only make the control; their callers wrap it with field() (checked below)."""
    helpers = {"textInput", "numberInput"}
    for name, body in ds.functions(ds.script_text()).items():
        text = ds.strip_comments(body)
        if not re.search(r"el\('(input|select|textarea)'", text) or name in helpers:
            continue
        assert "htmlFor" in text or "aria-label" in text or "field(" in text, name
    for name, body in ds.functions(ds.script_text()).items():
        text = ds.strip_comments(body)
        if re.search(r"\b(textInput|numberInput)\(", text) and name not in helpers:
            assert "field(" in text, name
def test_action_button_refuses_to_build_a_nameless_button():
    body = ds.strip_comments(ds.functions(ds.script_text())["actionButton"])
    assert re.search(r"typeof label !== 'string' \|\| !label\.trim\(\)", body)
    assert "throw new Error(" in body
def test_focus_ring_is_kept_and_covers_every_native_control():
    page_text = ds.load_page()
    assert not re.search(r"outline\s*:\s*(none|0)\b", page_text)
    rule = re.search(r"([^{}]*:focus-visible[^{}]*)\{([^}]*)\}", page_text)
    assert rule and "outline: 3px solid" in rule.group(2)
    selectors = rule.group(1)
    for sel in ("button", "input", "select", "textarea", "summary"):
        assert f"{sel}:focus-visible" in selectors, sel
def test_no_positive_tabindex_and_native_controls_only():
    assert page().positive_tabindex == []
    script = ds.strip_comments(ds.script_text())
    assert not re.search(r"el\('(div|span|li|td|tr)'[^;\n]*\)\.onclick", script)
    assert 'role="button"' not in ds.load_page() and "role', 'button'" not in script
def test_state_labels_cover_every_state_and_status():
    script = ds.strip_comments(ds.script_text())
    table = re.search(r"const STATE_LABELS = \{(.*?)\n\};", script, re.S).group(1)
    expected = {
        "service": {"active", "idle", "never_seen"},
        "source": {"running", "missing", "unreadable", "error", "stopped", "unknown"},
        "rule": {"active", "suspended", "revoked", "expired"},
        "proposal": {"pending", "approved", "denied", "done", "withdrawn"},
    }
    for kind, values in expected.items():
        row = re.search(rf"{kind}: \{{(.*?)\}}", table).group(1)
        got = set(re.findall(r"(\w+):", row))
        assert got == values, (kind, got)
    from chimericmesh.approvals.models import RULE_STATUSES
    from chimericmesh.connectors.tailread import Status
    from chimericmesh.services.status import State
    import typing
    assert set(RULE_STATUSES) == expected["rule"]
    assert set(typing.get_args(State)) == expected["service"]
    assert set(typing.get_args(Status)) - {"busy"} <= expected["source"]  # busy turns into unreadable before it is shown
    assert "stateLabel" in ds.functions(pure_block())
    text = cases_text()
    for kind in expected:
        assert f"args: ['{kind}'," in text, kind
def test_status_is_text_not_only_colour():
    """A state shown with a colour class also carries its text label (R8.2)."""
    script = ds.strip_comments(ds.script_text())
    for name in ("serviceCard", "renderProposalCard", "ruleRow"):
        assert "stateLabel(" in ds.strip_comments(ds.functions(script)[name]), name
    assert "'AUTO-APPROVED'" in script and "'built in'" in script
def test_narrow_layout_rules_exist():
    page_text = ds.load_page()
    assert re.search(r"@media \(max-width: 480px\) \{", page_text)
    assert "grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr))" in page_text
    assert re.search(r"\.svc-card[^{]*\{[^}]*overflow-wrap: anywhere", page_text)
    assert re.search(r'<meta name="viewport" content="width=device-width, initial-scale=1">', page_text)
def test_only_the_rules_and_audit_tables_scroll_sideways():
    p = page()
    assert p.wrappers == ["table-wrap", "table-wrap"], p.wrappers  # the rules table and the audit table
    page_text = ds.load_page()
    assert len(re.findall(r"overflow-x: auto", page_text)) == 1 and ".table-wrap { overflow-x: auto; }" in page_text
    seg = page_text[page_text.index('id="svcList"'): page_text.index('id="svcList"') + 80]
    assert "table-wrap" not in seg
def test_motion_respects_the_reduced_motion_setting():
    page_text = ds.load_page()
    script = ds.strip_comments(ds.script_text())
    css = page_text.split("</style>")[0]
    uses = [ln for ln in css.splitlines() if re.search(r"\banimation\s*:", ln)]
    assert len(uses) == 1 and "prefers-reduced-motion: no-preference" in uses[0], uses  # the only animation is opt-in
    assert "scroll-behavior" not in page_text
    for m in re.finditer(r"behavior: ([^,}]+)", script):
        assert "prefers-reduced-motion: reduce" in script[max(0, m.start() - 200): m.end()], m.group(0)
    assert "@media (prefers-reduced-motion: no-preference) { .pulse {" in page_text
def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i: i + 2], 16) / 255 for i in (0, 2, 4))
def luminance(h: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
def test_text_colours_reach_the_aa_contrast_on_the_backgrounds_they_use():
    css = ds.load_page().split("</style>")[0]
    var = dict(re.findall(r"--([a-z]+): (#[0-9a-fA-F]{6})", css))
    backgrounds = [var["bg"], var["panel"], "#0f0d17", "#08070d"]  # page, panel, card and log backgrounds used in the CSS
    for fg in ("ink", "dim", "ghost", "amber", "blood", "violet"):
        for bg in backgrounds:
            assert contrast(var[fg], bg) >= 4.5, (fg, bg, round(contrast(var[fg], bg), 2))
    assert contrast(var["bg"], var["ember"]) >= 4.5  # the filled buttons (dark text on the ember fill)
    assert contrast("#ffd6de", "#2a0f18") >= 4.5  # the toast
    assert contrast(var["amber"], "#2a1d08") >= 4.5  # the paused banner

# ---- review-1 notes that belong to this task (L3, L4, L5) ---------------------------------------------------
def test_repeated_button_names_carry_the_item_in_an_aria_label():
    """L3: 'Open', 'Edit', 'Approve', 'Pause' repeat on every card or row; a screen reader needs the item's name."""
    script = ds.strip_comments(ds.script_text())
    fns = ds.functions(script)
    assert "setAttribute('aria-label', ariaLabel)" in ds.strip_comments(fns["focusKeyButton"])
    assert "setAttribute('aria-label', aria)" in ds.strip_comments(fns["actionButton"])
    card = ds.strip_comments(fns["serviceCard"])
    assert "`Open ${s.name}`" in card and "`Edit ${s.name}`" in card
    prop = ds.strip_comments(fns["renderProposalCard"])
    for text in ("`Approve proposal ${p.id}`", "`Deny proposal ${p.id}`", "`Withdraw proposal ${p.id}`"):
        assert text in prop, text
    row = ds.strip_comments(fns["ruleRow"])
    for text in ("`Pause rule ${r.id}`", "`Revoke rule ${r.id}`", "`Re-enable rule ${r.id}`", "`Edit rule ${r.id}`"):
        assert text in row, text
def test_the_slash_shortcut_only_works_on_the_overview_where_the_terminal_is_visible():
    """L4"""
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"if \(ev\.key !== '/' \|\| ev\.ctrlKey \|\| ev\.metaKey \|\| ev\.altKey \|\| STATE\.route\.view !== 'overview'\) return;", script)
def test_returning_to_the_overview_refreshes_it_at_once():
    """L5"""
    script = ds.strip_comments(ds.script_text())
    assert re.search(r"ENTER\.overview = \(\) => \{ refresh\(\); fetchChat\(\); \};", script)


def test_rules_table_cells_keep_a_minimum_width():
    """Review 2 (browser check): with `overflow-wrap: anywhere` alone, "r1" and "active" wrapped letter by letter."""
    from dashboard_static import load_page
    cell = [ln for ln in load_page().splitlines() if ln.strip().startswith("table.rules th, table.rules td")]
    assert len(cell) == 1 and "min-width: 10ch" in cell[0]


def test_live_log_filter_row_aligns_controls_and_keeps_checkbox_inline():
    page_text = ds.load_page()
    assert ".live-ctl { align-items: flex-end;" in page_text
    assert re.search(r'\.live-ctl \.field\.inline \{[^}]*flex-direction: row;[^}]*align-items: center', page_text)
    assert 'class="field inline"><label for="liveAuto">' in page_text
    assert 'class="field"><label for="liveSev">' in page_text and 'class="field"><label for="liveText">' in page_text
    assert re.search(r'\.live-ctl select, \.live-ctl input\[type=text\] \{[^}]*height: 40px', page_text)
