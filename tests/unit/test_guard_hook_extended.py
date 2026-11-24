"""The widened innerHTML guard hook (dashboard task 1.2). test_guard_hook.py is not edited."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".kiro" / "hooks" / "guard_innerhtml.py"
WEB = "e:\\Projects\\Kiroween-2026\\src\\chimericmesh\\web\\index.html"


def run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True)


def payload(path: str, **fields: str) -> str:
    return json.dumps({"tool_name": "fs_write", "tool_input": {"path": path, **fields}})


BLOCKED = [
    "x.innerHTML += y",
    "x.innerHTML ||= y",
    "x['innerHTML'] = y",
    'x["outerHTML"] = y',
    "x[ 'outerHTML' ] += y",
    "document.writeln(y)",
    "range.createContextualFragment(y)",
    "iframe.srcdoc = y",
    "new DOMParser()",
    "new  DOMParser ()",
    # the original four forms
    "el.innerHTML = y",
    "el.outerHTML = y",
    "el.insertAdjacentHTML('beforeend', y)",
    "document.write(y)",
]

ALLOWED = [
    "x.textContent = y",
    "el.setAttribute('aria-label', y)",
    "// never use DOMParser",
    "var s = 'srcdoc is forbidden';",
    "// createContextualFragment is not used here",
    "if (a.innerHTML === b) {}",
]


@pytest.mark.parametrize("text", BLOCKED)
def test_blocked_in_web_for_text_and_new_str(text):
    for field in ("text", "newStr"):
        r = run(payload(WEB, **{field: text}))
        assert r.returncode == 2, (field, text)
        assert "textContent" in r.stderr


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed_in_web(text):
    r = run(payload(WEB, text=text))
    assert r.returncode == 0, text
    assert r.stderr == ""


@pytest.mark.parametrize("text", BLOCKED)
def test_same_text_is_allowed_outside_web(text):
    assert run(payload("tests/unit/test_dash.py", text=text)).returncode == 0


def test_bad_input_is_still_ignored():
    assert run("").returncode == 0
    assert run("{not json").returncode == 0
    assert run(json.dumps({"tool_input": None})).returncode == 0
    assert run(json.dumps([1, 2])).returncode == 0
