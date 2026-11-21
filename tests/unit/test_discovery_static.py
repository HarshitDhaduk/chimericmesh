"""Static safety scans of the discovery package (R2.1f, R2.2, R2.3, review M3 and L5)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "chimericmesh" / "discovery"
TESTS = ROOT / "tests"
NETWORK = {"socket", "ssl", "http", "urllib", "httpx", "ftplib", "smtplib", "telnetlib", "xmlrpc"}
OPEN_ALLOWED = {"probe.py", "catalog.py"}
THREAD_SIDE = {"engine.py", "match.py", "collect.py", "probe.py", "runner.py"}
BANNED_CALLS = {"open_connection", "create_connection", "getaddrinfo", "create_subprocess_shell",
                "system", "popen", "execv", "execve", "execl", "execlp", "execvp", "execvpe"}
READERS = {"open", "read_text", "read_bytes"}


def _files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _tops(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def test_package_exists_and_has_files():
    assert (PKG / "__init__.py").exists()


def test_no_network_imports():
    for path in _files():
        bad = _tops(ast.parse(path.read_text(encoding="utf-8"))) & NETWORK
        assert not bad, f"{path.name} imports {bad}"


def test_no_file_reads_outside_probe_and_catalog():
    for path in _files():
        if path.name in OPEN_ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) in READERS:
                raise AssertionError(f"{path.name}:{node.lineno} reads a file")


def test_no_shell_exec_or_connection_calls():
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            assert _call_name(node) not in BANNED_CALLS, f"{path.name}:{node.lineno}"
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, ast.Constant) and kw.value.value is True), \
                        f"{path.name}:{node.lineno} shell=True"


def test_thread_side_files_import_no_store_or_services():
    for name in THREAD_SIDE:
        path = PKG / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [("." * node.level) + (node.module or "")]
                if node.level and not node.module:
                    mods = [a.name for a in node.names]
            for mod in mods:
                low = mod.lstrip(".")
                assert not low.startswith(("chimericmesh.store", "chimericmesh.services", "store", "services")), \
                    f"{name} imports {mod}"
                assert not ("services" in low.split(".") or "store" in low.split(".")), f"{name} imports {mod}"


def test_no_proposal_style_ids_in_sources_or_tests():
    pattern = re.compile(r"p\d+-[A-Za-z0-9]+")
    files = _files() + sorted(TESTS.rglob("test_discovery_*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.name} has a proposal-style id"


def test_catalog_directory_holds_data_only():
    catalog = ROOT / "src" / "chimericmesh" / "catalog"
    if catalog.exists():
        assert not list(catalog.rglob("*.py"))
