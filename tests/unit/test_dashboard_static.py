"""Static safety checks on src/chimericmesh/web/index.html (dashboard task 1.3, R1.1, R1.2, R1.5).

They pass on the page of main and guard every later page task.
"""
from __future__ import annotations

import re

import pytest

import dashboard_static as ds

EXISTING_IDS = (
    "stats guide guideToggle guideProgress guideBar guideFill guideSteps guideStatus incPanel incidents "
    "propPanel propNote proposals termPanel transcript newMsgs termForm termInput termClear chips identity "
    "evPanel events ev-h sendForm source line storm toast"
).split()

ALLOWED_ATTRS = {
    "role", "type", "title", "disabled", "hidden", "for", "id", "name", "min", "max", "step", "maxlength",
    "autocomplete", "placeholder", "value", "rows",
}
ALLOWED_ATTR_PREFIXES = ("aria-", "data-")
SINK_WORDS = [
    r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\s*\.\s*write(ln)?\b", r"DOMParser",
    r"createContextualFragment", r"srcdoc", r"\beval\s*\(", r"new\s+Function\b",
]


def code() -> str:
    return ds.strip_comments(ds.script_text())


# ---- the helper itself ------------------------------------------------------------------------

def test_strip_comments_keeps_strings_and_regexes():
    src = "var a = 'x // not a comment'; // real\nvar r = /^\\/api\\//; /* block */ var b = \"/* no */\";\n"
    out = ds.strip_comments(src)
    assert "'x // not a comment'" in out and "real" not in out and "block" not in out
    assert "/^\\/api\\//" in out and '"/* no */"' in out


def test_functions_splits_top_level_bodies_with_braces_in_strings():
    src = "function a(x) { return '}' + `${x}`; }\nasync function b() { if (1) { return 2; } }\nconst c = () => { function inner() {} };\n"
    f = ds.functions(src)
    assert set(f) == {"a", "b"}
    assert f["b"].endswith("}") and "inner" not in f


def test_code_only_blanks_strings():
    assert "innerHTML" not in ds.code_only("a = 'innerHTML'; // innerHTML\nb = 1;")


# ---- the page -----------------------------------------------------------------------------------

def test_page_parses_with_lang_and_known_ids():
    p = ds.html_parser()
    html_tags = [a for t, a in p.tags if t == "html"]
    assert html_tags and html_tags[0].get("lang")
    missing = [i for i in EXISTING_IDS if i not in p.ids]
    assert not missing, missing
    assert len(p.ids) == len(set(p.ids)), "duplicate ids"


def test_no_html_injection_sinks_outside_comments():
    text = code()
    for pat in SINK_WORDS:
        assert not re.search(pat, text), pat


def test_set_attribute_uses_literal_allowed_names():
    text = code()
    for m in re.finditer(r"setAttribute\s*\(\s*([^,)]*?)\s*(?:,\s*([^)]*))?\)", text):
        first, second = m.group(1), m.group(2) or ""
        lit = re.fullmatch(r"(['\"])([a-z-]+)\1", first)
        assert lit, f"setAttribute first argument is not a literal: {m.group(0)}"
        name = lit.group(2)
        assert name in ALLOWED_ATTRS or name.startswith(ALLOWED_ATTR_PREFIXES), name
        if name in ("id", "name"):
            assert re.match(r"\s*(['\"])", second), f"setAttribute({name}) needs a literal value: {m.group(0)}"


def test_no_url_style_or_event_attribute_is_set_from_code():
    text = code()
    assert not re.search(r"\.(href|src)\s*=(?!=)", text)
    assert "style.cssText" not in text
    assert not re.search(r"setAttribute\s*\(\s*['\"]on", text)
    for tag, attrs in ds.html_parser().tags:
        bad = [a for a in attrs if a.startswith("on")]
        assert not bad, (tag, bad)


def test_no_external_origin_and_fetch_only_through_the_helper():
    page = ds.load_page()
    assert "http://" not in page and "https://" not in page
    assert "@import" not in page
    for tag, attrs in ds.html_parser().tags:
        if tag == "script":
            assert not attrs.get("src")
        if tag == "link":
            assert (attrs.get("href") or "").startswith("data:"), attrs
    text = code()
    # Allow-list: the page's fetch helper (today the arrow `const api`, later `request`) calls `fetch(path, ...)`
    # with the one parameter named `path`; every other call must pass a literal path starting with `/` (R1.1).
    # The helper's own guard (path must start with /api/ or be /) is checked in test_dashboard_core.py.
    for m in re.finditer(r"\bfetch\s*\(", text):
        tail = text[m.end(): m.end() + 80]
        assert re.match(r"\s*(?:path\s*,|(['\"`])/)", tail), f"fetch outside the request helper: {tail!r}"

def test_local_storage_only_for_the_guide_preference_and_no_cookie():
    text = code()
    uses = re.findall(r"\blocalStorage\b", text)
    ok = re.findall(r"\blocalStorage\.(?:getItem|setItem)\(\s*(?:PREF_KEY|'cm\.guide\.pref')", text)
    assert len(uses) == len(ok)
    if uses:
        assert re.search(r"const\s+PREF_KEY\s*=\s*'cm\.guide\.pref'", text)
    assert "document.cookie" not in text


def test_focus_outline_is_never_removed():
    assert not re.search(r"outline\s*:\s*(none|0)\b", ds.load_page())


def test_guided_workflow_functions_match_the_baseline():
    """The guide code is not changed by this module (design, "Overview, terminal, guide")."""
    for name, digest in ds.BASELINE_HASHES.items():
        assert ds.function_hash(name) == digest, name


@pytest.mark.parametrize("name", sorted(ds.BASELINE_HASHES))
def test_baseline_hashes_are_sha256_hex(name):
    assert re.fullmatch(r"[0-9a-f]{64}", ds.BASELINE_HASHES[name])
