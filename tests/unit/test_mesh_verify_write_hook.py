"""The optional PreToolUse hook ``.kiro/hooks-optional/mesh_verify_write.py`` (agent-loop task 10; R5.9).

The script is loaded by path (as ``test_guard_hook.py`` runs its script) and driven through ``decide`` with a
fake hub, plus a few real subprocess runs for the exit codes of ``main``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from chimericmesh.agent.verify import VerifyInputs, evaluate_verify

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".kiro" / "hooks-optional" / "mesh_verify_write.py"
DEFINITION = REPO / ".kiro" / "hooks-optional" / "mesh-verify-write.json"
GUARD = REPO / ".kiro" / "hooks" / "guard-innerhtml.json"
STATE = ".chimericmesh-active-proposal"
NOTE = "no code folder, path scope not checked"


def load():
    spec = importlib.util.spec_from_file_location("mesh_verify_write", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load()


_DEFAULT = object()


class FakeHub:
    def __init__(self, reply=_DEFAULT, error: Exception | None = None):
        self.reply = {"ok": True, "reasons": []} if reply is _DEFAULT else reply
        self.error = error
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, url: str, body: bytes, timeout: float):
        self.calls.append((url, body, timeout))
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture
def root(tmp_path):
    (tmp_path / STATE).write_text("p4\n", encoding="utf-8")
    return str(tmp_path)


def stdin(path="src/app.py", **extra) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "fs_write", "tool_input": {"path": path, **extra}})


def run(text, root, hub, env=None):
    return hook.decide(text, cwd=root, env={} if env is None else env, post=hub)


def test_ok_true_exits_zero_and_posts_the_absolute_path(root):
    hub = FakeHub()
    code, err = run(stdin("src/app.py"), root, hub)
    assert (code, err) == (0, "")
    (url, body, timeout), = hub.calls
    assert url == "http://127.0.0.1:8000/api/proposals/p4/verify"
    assert json.loads(body) == {"files": [os.path.abspath(os.path.join(root, "src", "app.py"))]}
    assert timeout <= 5


def test_ok_false_exits_two_with_the_first_reason(root):
    code, err = run(stdin(), root, FakeHub({"ok": False, "reasons": ["status is pending", "other"]}))
    assert code == 2
    assert "status is pending" in err and "other" not in err


@pytest.mark.parametrize("content", ["", "   \n", "p0", "p-1", "../x", "x" * 5000, "p01", "p4 p5", "P4"])
def test_a_bad_state_file_blocks(tmp_path, content):
    (tmp_path / STATE).write_text(content, encoding="utf-8")
    hub = FakeHub()
    assert run(stdin(), str(tmp_path), hub)[0] == 2
    assert hub.calls == []


def test_a_missing_state_file_blocks(tmp_path):
    hub = FakeHub()
    code, err = run(stdin(), str(tmp_path), hub)
    assert code == 2 and STATE in err and hub.calls == []


def test_an_unreachable_hub_blocks(root):
    assert run(stdin(), root, FakeHub(error=OSError("refused")))[0] == 2


@pytest.mark.parametrize("reply", ["text", None, [], 7, {"ok": "true"}, {"ok": 1}, {"reasons": []}, {"ok": None}])
def test_a_reply_that_is_not_ok_true_blocks(root, reply):
    assert run(stdin(), root, FakeHub(reply))[0] == 2


def test_a_reply_that_is_not_json_blocks(root):
    # the real ``post`` raises ValueError for a body that is not JSON
    assert run(stdin(), root, FakeHub(error=ValueError("not json")))[0] == 2


def test_the_no_code_folder_note_blocks_even_when_ok(root):
    code, err = run(stdin(), root, FakeHub({"ok": True, "reasons": [NOTE]}))
    assert code == 2 and NOTE in err


@pytest.mark.parametrize("url", [
    "http://example.com", "http://127.0.0.1.evil.com", "http://127.0.0.1@evil.com", "https://127.0.0.1:8000",
    "http://localhost.evil.com:8000", "http://[::2]:8000", "ftp://127.0.0.1", "http://127.0.0.1:8000/x",
    "http://user:pw@127.0.0.1:8000", "http://127.0.0.1:99999"])
def test_a_non_loopback_or_odd_url_blocks_without_a_call(root, url):
    hub = FakeHub()
    code, _ = run(stdin(), root, hub, {"CHIMERICMESH_URL": url})
    assert code == 2 and hub.calls == []


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:9000", "http://localhost:9000"), ("http://127.0.0.1:8123/", "http://127.0.0.1:8123"),
    ("http://[::1]:8000", "http://[::1]:8000")])
def test_loopback_urls_are_used(root, url, expected):
    hub = FakeHub()
    assert run(stdin(), root, hub, {"CHIMERICMESH_URL": url})[0] == 0
    assert hub.calls[0][0] == expected + "/api/proposals/p4/verify"


def test_a_write_to_the_state_file_is_allowed_without_a_call(root):
    hub = FakeHub()
    assert run(stdin(STATE), root, hub) == (0, "")
    assert run(stdin(os.path.join(root, STATE)), root, hub) == (0, "")
    assert hub.calls == []


def test_the_state_file_name_elsewhere_is_not_exempt(root):
    hub = FakeHub({"ok": False, "reasons": ["no"]})
    assert run(stdin("sub/" + STATE), root, hub)[0] == 2


def test_the_state_file_is_dropped_from_a_mixed_list(root):
    hub = FakeHub()
    run(json.dumps({"tool_input": {"paths": [STATE, "a.py"]}}), root, hub)
    assert json.loads(hub.calls[0][1])["files"] == [os.path.abspath(os.path.join(root, "a.py"))]


def test_a_relative_target_is_judged_against_the_workspace_not_the_code_folder(tmp_path):
    (tmp_path / STATE).write_text("p4", encoding="utf-8")
    code_folder = str(tmp_path / "examples" / "legacy_app")
    os.makedirs(code_folder)

    def fake_hub(url, body, timeout):
        inp = VerifyInputs(proposal_id="p4", status="approved", approved_by_rule=None, declared_files=None,
                           approval={"by": "user", "rule_id": None, "rule_status_now": None, "approved_at": 1.0,
                                     "expired": False},
                           rule_status=None, rule_code_folder=None, rule_expires_at=None, paused=False,
                           approvals_available=True, code_folder=code_folder, has_service=True, now=2.0,
                           protected_dirs=())
        result = evaluate_verify(inp, json.loads(body)["files"])
        return {"ok": result.ok, "reasons": list(result.reasons)}

    root = str(tmp_path)
    assert run(stdin("src/chimericmesh/hub.py"), root, fake_hub)[0] == 2
    assert run(stdin("examples/legacy_app/upstream.py"), root, fake_hub)[0] == 0


@pytest.mark.parametrize("text", ["", "{not json", "[]", "null", "7", '"x"', "{}", '{"tool_input": null}',
                                  '{"tool_input": {}}', '{"tool_input": {"path": 5}}', '{"tool_input": {"path": ""}}',
                                  '{"tool_input": {"paths": "x"}}', '{"tool_input": {"path": "a\\u0000b"}}'])
def test_unparseable_or_pathless_input_exits_zero(root, text):
    hub = FakeHub()
    assert run(text, root, hub)[0] == 0
    assert hub.calls == []


def test_more_targets_than_verify_accepts_are_still_judged_not_cut_off(root):
    """Review 3 M1: a long ``paths`` list must not hide a bad target behind the first 20."""
    seen = []

    def hub(url, body, timeout):
        files = json.loads(body)["files"]
        seen.append(len(files))
        return {"ok": len(files) <= 20, "reasons": ["a write names 1 to 20 files"]}

    paths = [f"f{i}.py" for i in range(40)]
    assert run(json.dumps({"tool_input": {"paths": paths}}), root, hub)[0] == 2
    assert seen == [21]

def test_the_file_path_key_is_read(root):
    hub = FakeHub()
    run(json.dumps({"tool_input": {"file_path": "x.py"}}), root, hub)
    assert len(hub.calls) == 1


def test_body_has_only_files_and_no_header_carries_a_token(root):
    hub = FakeHub()
    run(stdin(), root, hub)
    assert list(json.loads(hub.calls[0][1])) == ["files"]
    assert len(hub.calls[0]) == 3          # url, body, timeout: the injected post has no header argument


def test_decide_never_raises_on_odd_input(root):
    for text in ("\x00", "{" * 5000, json.dumps({"tool_input": {"path": "a" * 100000}})):
        code, err = run(text, root, FakeHub())
        assert code in (0, 2) and isinstance(err, str)


def test_the_script_reads_no_token_and_opens_only_the_state_file():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "admin.token" not in source and "Authorization" not in source and "X-Admin" not in source
    assert len(re.findall(r"(?<![.\w])open\(", source)) == 1
    assert "subprocess" not in source and "os.system" not in source


def test_the_script_is_stdlib_only():
    tree = SCRIPT.read_text(encoding="utf-8")
    for line in tree.splitlines():
        m = re.match(r"\s*(?:from|import)\s+([A-Za-z_][\w.]*)", line)
        if m:
            assert m.group(1).split(".")[0] in sys.stdlib_module_names or m.group(1) == "__future__", line


# ---- main: the exit codes the process returns -----------------------------------------------------

def run_process(text, cwd, env_extra=None):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    env.pop("CHIMERICMESH_URL", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT)], input=text, capture_output=True, text=True, cwd=cwd,
                          env=env, timeout=30)


def test_process_exits_zero_for_bad_input(root):
    assert run_process("", root).returncode == 0
    assert run_process("{not json", root).returncode == 0


def test_process_blocks_when_the_hub_is_down(root):
    # port 9 (discard) on loopback: nothing listens there; the connection is refused at once
    r = run_process(stdin(), root, {"CHIMERICMESH_URL": "http://127.0.0.1:9"})
    assert r.returncode == 2 and r.stderr.strip()


def test_process_blocks_a_foreign_url_without_connecting(root):
    r = run_process(stdin(), root, {"CHIMERICMESH_URL": "http://example.com"})
    assert r.returncode == 2


# ---- the definition and the install notes ---------------------------------------------------------

def test_the_definition_matches_the_existing_guard():
    data = json.loads(DEFINITION.read_text(encoding="utf-8"))
    guard = json.loads(GUARD.read_text(encoding="utf-8"))
    assert data["version"] == "v1" and len(data["hooks"]) == 1
    entry = data["hooks"][0]
    assert entry["trigger"] == "PreToolUse"
    assert entry["matcher"] == r"^(fs_write|fs_append|str_replace)$" == guard["hooks"][0]["matcher"]
    assert entry["action"]["type"] == "command"
    assert entry["action"]["command"] == "python .kiro/hooks/mesh_verify_write.py"
    assert "timeout" not in entry["action"]
    assert isinstance(entry["name"], str) and entry["name"]


def test_the_hook_files_are_opt_in_only():
    hooks = REPO / ".kiro" / "hooks"
    assert not (hooks / "mesh-verify-write.json").exists()
    assert not (hooks / "mesh_verify_write.py").exists()
    assert SCRIPT.is_file() and DEFINITION.is_file()
