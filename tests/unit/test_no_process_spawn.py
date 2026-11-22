"""R4.10: the hub starts no process. Only ``fsperms.py`` and ``discovery/runner.py`` may use a process API
(discovery A1: the narrow rule), and nothing in ``src/chimericmesh`` imports ``kiro`` or builds a
``kiro-cli`` command line (F1, F3). The scanner works on the AST, so a comment or a docstring is not a hit."""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"

# Listed explicitly: a new file that spawns a process fails this test.
ALLOWED_FILES = {"fsperms.py", "discovery/runner.py"}

_OS_SPAWN = re.compile(r"^(system|popen|spawn\w*|exec\w*|posix_spawn\w*|startfile|fork\w*)$")
_ASYNCIO_SPAWN = {"create_subprocess_exec", "create_subprocess_shell"}
_PROCESS_MODULES = {"subprocess", "pty", "multiprocessing"}
_KIRO_CLI = re.compile(r"\bkiro[-_ ]cli\b", re.IGNORECASE)


def spawn_uses(source: str) -> list[str]:
    """Every use of a process-spawning API in ``source`` (names only)."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.split(".")[0] in _PROCESS_MODULES]
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in _PROCESS_MODULES:
                hits.append(mod)
            elif mod == "os":
                hits += [f"os.{a.name}" for a in node.names if _OS_SPAWN.match(a.name)]
            elif mod == "asyncio":
                hits += [f"asyncio.{a.name}" for a in node.names if a.name in _ASYNCIO_SPAWN]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and _OS_SPAWN.match(node.attr):
                hits.append(f"os.{node.attr}")
            elif node.value.id == "asyncio" and node.attr in _ASYNCIO_SPAWN:
                hits.append(f"asyncio.{node.attr}")
            elif node.value.id in _PROCESS_MODULES:
                hits.append(f"{node.value.id}.{node.attr}")
    return hits


def kiro_uses(source: str) -> list[str]:
    """An import of a ``kiro`` module, or a string constant (not a docstring) naming ``kiro-cli``."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.split(".")[0].lower().startswith("kiro")]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0].lower().startswith("kiro"):
                hits.append(node.module or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if _KIRO_CLI.search(node.value):
                hits.append(node.value[:40])
    return hits


def _files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_the_listed_files_use_a_process_api():
    offenders: dict[str, list[str]] = {}
    for path in _files():
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED_FILES:
            continue
        found = spawn_uses(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel] = found
    assert not offenders, f"process API outside the allow-list: {offenders}"


def test_the_allow_list_is_exact():
    """Both allowed files exist and really use the API, so a stale entry does not hide a gap."""
    for rel in ALLOWED_FILES:
        assert spawn_uses((SRC / rel).read_text(encoding="utf-8")), rel


def test_hub_mcp_tailer_parsers_and_agent_have_none():
    names = ("hub.py", "mcp_server.py", "connectors/tailer.py", "connectors/tailread.py", "listeners.py")
    for rel in names:
        assert spawn_uses((SRC / rel).read_text(encoding="utf-8")) == [], rel
    for path in list((SRC / "parsers").glob("*.py")) + list((SRC / "agent").glob("*.py")):
        assert spawn_uses(path.read_text(encoding="utf-8")) == [], path.name


def test_no_file_imports_kiro_or_builds_a_kiro_cli_command():
    offenders = {p.relative_to(SRC).as_posix(): kiro_uses(p.read_text(encoding="utf-8")) for p in _files()}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_scanner_detects_every_api():
    for line in (
        "import subprocess", "from subprocess import Popen", "import os\nos.system('x')",
        "import os\nos.popen('x')", "import os\nos.spawnl(0, 'x')", "import os\nos.execv('x', [])",
        "from os import system", "import asyncio\nasyncio.create_subprocess_exec('x')",
        "import asyncio\nasyncio.create_subprocess_shell('x')", "from asyncio import create_subprocess_exec",
        "import multiprocessing",
    ):
        assert spawn_uses(line), line
    assert spawn_uses("import os\nos.path.join('a')\nimport asyncio\nasyncio.sleep(1)") == []
    assert spawn_uses("# subprocess\nx = 'os.system'") == []


def test_kiro_scanner_detects_import_and_command_line_but_not_docstrings():
    assert kiro_uses("import kiro")
    assert kiro_uses("from kiro_cli import run")
    assert kiro_uses("cmd = ['kiro-cli', 'chat']")
    assert kiro_uses('def f():\n    """runs kiro-cli"""\n    return 1') == []
    assert kiro_uses("x = 1") == []
