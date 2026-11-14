import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / ".kiro" / "hooks" / "guard_innerhtml.py"
WEB = "e:\\Projects\\Kiroween-2026\\src\\chimericmesh\\web\\index.html"


def run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True
    )


def payload(path: str, **fields: str) -> str:
    return json.dumps({"tool_name": "fs_write", "tool_input": {"path": path, **fields}})


def test_blocks_innerhtml_in_web():
    r = run(payload(WEB, text="el.innerHTML = msg;"))
    assert r.returncode == 2
    assert "textContent" in r.stderr


def test_blocks_str_replace_new_str():
    r = run(payload(WEB, newStr="el.insertAdjacentHTML('beforeend', x)"))
    assert r.returncode == 2


def test_allows_textcontent_in_web():
    r = run(payload(WEB, text="el.textContent = msg;"))
    assert r.returncode == 0
    assert r.stderr == ""


def test_allows_non_web_file():
    r = run(payload("tests/unit/test_dash.py", text="assert 'el.innerHTML = x' not in src"))
    assert r.returncode == 0


def test_bad_input_is_ignored():
    assert run("").returncode == 0
    assert run("{not json").returncode == 0
