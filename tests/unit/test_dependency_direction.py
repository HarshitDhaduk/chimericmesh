"""Table-driven dependency-direction test (capability map, "Dependency direction").

Every ``.py`` file under ``src/chimericmesh/`` must have a group; its imports (including those
under ``TYPE_CHECKING``) must be allowed by the "may import" table; ``core`` and ``store`` import no
third-party package; ``httpx`` is imported only by ``wiring`` files.

Follow-ups for the next map revision (the store design resolves them, the map still has the old text):
- the map's file-to-group table lacks ``notify.py``, ``replay.py``, ``replay_irc.py``, the root
  ``__init__.py`` and package ``__init__.py`` files; the table below is the complete assignment.
- B10 / ``auto-approval`` R4.8 list the reserved ``decided_by`` prefixes more widely than the store
  design (``rule:``, ``rule-``, ``withdrawn:``, ``expired:``, ``expired-``).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PKG = "chimericmesh"
SRC = Path(__file__).resolve().parents[2] / "src" / PKG

# Exact file (relative to src/chimericmesh, posix) or directory prefix ending in "/" -> group.
FILE_GROUPS: dict[str, str] = {
    "__init__.py": "core",
    "events.py": "core",
    "diagnostics.py": "core",
    "parsers/": "core",
    "store/": "store",
    "fsperms.py": "store",
    "services/": "services",
    "connectors/": "connectors",
    "notify.py": "wiring",
    "replay.py": "wiring",
    "replay_irc.py": "wiring",
    "hub.py": "wiring",
    "api.py": "wiring",
    "commands.py": "wiring",
    "listeners.py": "wiring",
    "mcp_server.py": "wiring",
    "__main__.py": "wiring",
}

ALL_GROUPS = [
    "core", "store", "services", "connectors", "discovery",
    "auto-approval", "agent-loop", "dashboard", "wiring",
]

# A group may always import itself.
MAY_IMPORT: dict[str, set[str]] = {
    "core": {"core"},
    "store": {"core", "store"},
    "services": {"core", "store", "services"},
    "connectors": {"core", "store", "services", "connectors"},
    "discovery": {"core", "store", "services", "discovery"},
    "auto-approval": {"core", "store", "services", "auto-approval"},
    "agent-loop": {"core", "store", "services", "auto-approval", "agent-loop"},
    "dashboard": {"core", "dashboard"},
    "wiring": set(ALL_GROUPS),
}

THIRD_PARTY_ALLOWED_GROUPS = {"wiring"}

# Per-file allow-list (services, task 1.3): only the HTTP layer of a module may import the web stack.
THIRD_PARTY_FILES: dict[str, set[str]] = {
    "services/deps.py": {"fastapi", "starlette", "pydantic"},
    "services/routes.py": {"fastapi", "starlette", "pydantic"},
}


def group_of(rel: str) -> str | None:
    if rel in FILE_GROUPS:
        return FILE_GROUPS[rel]
    for key, group in FILE_GROUPS.items():
        if key.endswith("/") and rel.startswith(key):
            return group
    return None


def _module_rel(parts: list[str]) -> str:
    """Map a dotted chimericmesh module (without the package name) to a file path for group lookup."""
    if not parts:
        return "__init__.py"
    candidate = "/".join(parts) + ".py"
    if (SRC / candidate).exists():
        return candidate
    if (SRC / "/".join(parts)).is_dir():
        return "/".join(parts) + "/__init__.py"
    # an attribute imported from a package or module: use the longest existing prefix
    return _module_rel(parts[:-1]) if len(parts) > 1 else "__init__.py"


def imports_of(path: Path) -> list[tuple[str, list[str] | None]]:
    """Return (top-level name, chimericmesh module parts or None) for every import in the file."""
    rel_parts = list(path.relative_to(SRC).with_suffix("").parts)
    package_parts = rel_parts[:-1]  # the package the file lives in (an __init__ lives in its own package)
    found: list[tuple[str, list[str] | None]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == PKG:
                    found.append((PKG, parts[1:]))
                else:
                    found.append((parts[0], None))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                mod = base + (node.module.split(".") if node.module else [])
                if node.module is None:
                    for alias in node.names:
                        found.append((PKG, mod + [alias.name]))
                else:
                    found.append((PKG, mod))
            else:
                parts = (node.module or "").split(".")
                if parts[0] == PKG:
                    if len(parts) == 1:
                        for alias in node.names:
                            found.append((PKG, [alias.name]))
                    else:
                        found.append((PKG, parts[1:]))
                else:
                    found.append((parts[0], None))
    return found


def all_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_file_has_a_group():
    missing = [p.relative_to(SRC).as_posix() for p in all_files() if group_of(p.relative_to(SRC).as_posix()) is None]
    assert not missing, f"add these files to FILE_GROUPS: {missing}"


def test_imports_follow_the_table():
    problems: list[str] = []
    for path in all_files():
        rel = path.relative_to(SRC).as_posix()
        group = group_of(rel)
        if group is None:
            continue
        allowed = MAY_IMPORT[group]
        for top, parts in imports_of(path):
            if top != PKG or parts is None:
                continue
            target = group_of(_module_rel(parts))
            if target is None:
                problems.append(f"{rel}: import of unknown module {PKG}.{'.'.join(parts)}")
            elif target not in allowed:
                problems.append(f"{rel} ({group}) imports {PKG}.{'.'.join(parts)} ({target})")
    assert not problems, "\n".join(problems)


def test_third_party_imports_only_in_wiring():
    stdlib = set(sys.stdlib_module_names)
    problems: list[str] = []
    for path in all_files():
        rel = path.relative_to(SRC).as_posix()
        group = group_of(rel)
        if group in THIRD_PARTY_ALLOWED_GROUPS:
            continue
        allowed_here = THIRD_PARTY_FILES.get(rel, set())
        for top, parts in imports_of(path):
            if top != PKG and top not in stdlib and top not in allowed_here:
                problems.append(f"{rel} ({group}) imports third-party {top}")
    assert not problems, "\n".join(problems)


def test_httpx_only_in_wiring_files():
    for path in all_files():
        rel = path.relative_to(SRC).as_posix()
        if any(top == "httpx" for top, _ in imports_of(path)):
            assert group_of(rel) == "wiring", rel


def test_root_init_imports_nothing_from_the_package():
    root = SRC / "__init__.py"
    assert [t for t, _ in imports_of(root) if t == PKG] == []


def test_no_group_other_than_wiring_imports_wiring_modules():
    wiring_modules = {"hub", "api", "commands", "listeners", "mcp_server"}
    for path in all_files():
        rel = path.relative_to(SRC).as_posix()
        if group_of(rel) == "wiring":
            continue
        for top, parts in imports_of(path):
            if top == PKG and parts and parts[0] in wiring_modules:
                pytest.fail(f"{rel} imports {parts[0]}")


def test_checker_detects_violations(tmp_path, monkeypatch):
    """The scanner itself: a store file importing hub, a core file importing httpx, TYPE_CHECKING counts."""
    pkg = tmp_path / PKG
    (pkg / "store").mkdir(parents=True)
    (pkg / "hub.py").write_text("")
    bad = pkg / "store" / "x.py"
    bad.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from chimericmesh.hub import Hub\n"
        "import httpx\nfrom .. import hub\n"
    )
    monkeypatch.setitem(globals(), "SRC", pkg)
    found = imports_of(bad)
    assert (PKG, ["hub"]) in found
    assert ("httpx", None) in found
    assert group_of(_module_rel(["hub"])) == "wiring"


def test_services_init_loads_no_web_layer():
    init = SRC / "services" / "__init__.py"
    imported = {".".join(parts) for top, parts in imports_of(init) if top == PKG and parts}
    assert not [m for m in imported if m.endswith(("services.deps", "services.routes"))]
    text = init.read_text(encoding="utf-8")
    assert "from .deps" not in text and "from .routes" not in text
    assert "import deps" not in text and "import routes" not in text


def _violations(root: Path) -> list[str]:
    """The scanner of this file, run on another tree (its logic with SRC swapped)."""
    global SRC
    saved = SRC
    SRC = root
    try:
        stdlib = set(sys.stdlib_module_names)
        out: list[str] = []
        for path in all_files():
            rel = path.relative_to(SRC).as_posix()
            group = group_of(rel)
            for top, parts in imports_of(path):
                if top == PKG and parts is not None:
                    target = group_of(_module_rel(parts))
                    if target is not None and target not in MAY_IMPORT[group]:
                        out.append(f"{rel} imports {'.'.join(parts)} ({target})")
                elif (top != PKG and top not in stdlib and group not in THIRD_PARTY_ALLOWED_GROUPS
                      and top not in THIRD_PARTY_FILES.get(rel, set())):
                    out.append(f"{rel} imports third-party {top}")
        return out
    finally:
        SRC = saved


@pytest.mark.parametrize(
    "line",
    [
        "from chimericmesh.hub import Hub",
        "import chimericmesh.connectors",
        "from chimericmesh import approvals",
        "import httpx",
        "import fastapi",
    ],
)
def test_services_file_with_forbidden_import_fails_the_scan(tmp_path, monkeypatch, line):
    pkg = tmp_path / PKG
    (pkg / "services").mkdir(parents=True)
    (pkg / "store").mkdir()
    (pkg / "store" / "__init__.py").write_text("")
    for name in ("hub.py", "connectors.py", "approvals.py"):
        (pkg / name).write_text("")
    monkeypatch.setitem(FILE_GROUPS, "connectors.py", "connectors")
    monkeypatch.setitem(FILE_GROUPS, "approvals.py", "auto-approval")
    (pkg / "services" / "x.py").write_text(line + "\n")
    assert _violations(pkg), line
    (pkg / "services" / "x.py").write_text("import os\nfrom chimericmesh.store import Store\n")
    assert _violations(pkg) == []
    # the web stack is allowed in services/routes.py only
    (pkg / "services" / "routes.py").write_text("import fastapi\n")
    assert _violations(pkg) == []
    (pkg / "services" / "asgi.py").write_text("import starlette\n")
    assert _violations(pkg) == ["services/asgi.py imports third-party starlette"]


# ---- connectors rules (connectors task 2.1, review M1) -------------------------------------------

CONNECTOR_OS_FILES = {"connectors/tailread.py", "connectors/tailer.py"}
CONNECTOR_SOCKET_FILES = {"connectors/manager.py"}
CONNECTOR_FORBIDDEN = {
    "fastapi", "starlette", "pydantic", "httpx", "urllib", "http", "smtplib", "ftplib", "subprocess",
}
PARSER_FORBIDDEN = {"os", "socket"}
SERVICES_WEB_MODULES = {"deps", "routes"}


def _file_rule_violations(root: Path) -> list[str]:
    """Per-file rules of the connectors module, run on any tree (``root`` replaces ``SRC``)."""
    global SRC
    saved = SRC
    SRC = root
    try:
        out: list[str] = []
        for path in all_files():
            rel = path.relative_to(SRC).as_posix()
            tops = {top for top, _ in imports_of(path)}
            if rel.startswith("connectors/"):
                bad = (tops & CONNECTOR_FORBIDDEN)
                if "socket" in tops and rel not in CONNECTOR_SOCKET_FILES:
                    bad.add("socket")
                if "os" in tops and rel not in CONNECTOR_OS_FILES:
                    bad.add("os")
                out += [f"{rel} imports {name}" for name in sorted(bad)]
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.ImportFrom):
                        mod = (node.module or "").split(".")
                        if mod[-1:] == ["services"]:
                            out += [f"{rel} imports services.{a.name}" for a in node.names
                                    if a.name in SERVICES_WEB_MODULES]
                        elif mod[-2:-1] == ["services"] and mod[-1] in SERVICES_WEB_MODULES:
                            out.append(f"{rel} imports services.{mod[-1]}")
                    elif isinstance(node, ast.Import):
                        out += [f"{rel} imports {a.name}" for a in node.names
                                if a.name.split(".")[-2:-1] == ["services"]
                                and a.name.split(".")[-1] in SERVICES_WEB_MODULES]
            elif rel.startswith("parsers/"):
                out += [f"{rel} imports {name}" for name in sorted(tops & PARSER_FORBIDDEN)]
        return out
    finally:
        SRC = saved


def test_connectors_per_file_rules_hold_on_the_tree():
    assert _file_rule_violations(SRC) == []


def test_connectors_package_has_a_group_row():
    assert group_of("connectors/__init__.py") == "connectors"
    assert group_of("connectors/manager.py") == "connectors"
    assert MAY_IMPORT["connectors"] == {"core", "store", "services", "connectors"}


def _tree(tmp_path: Path, rel: str, text: str, monkeypatch) -> Path:
    pkg = tmp_path / PKG
    for sub in ("connectors", "parsers", "services", "store"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    for name in ("hub.py", "listeners.py", "discovery.py", "approvals.py"):
        (pkg / name).write_text("")
    monkeypatch.setitem(FILE_GROUPS, "discovery.py", "discovery")
    monkeypatch.setitem(FILE_GROUPS, "approvals.py", "auto-approval")
    (pkg / rel).write_text(text)
    return pkg


@pytest.mark.parametrize(
    "line",
    [
        "import chimericmesh.hub",
        "from chimericmesh import listeners",
        "import chimericmesh.discovery",
        "import chimericmesh.approvals",
        "import httpx",
        "import subprocess",
        "import urllib.request",
        "import socket",
        "import os",
        "from chimericmesh.services import routes",
        "from chimericmesh.services.deps import x",
    ],
)
def test_connectors_file_with_forbidden_import_fails_the_scan(tmp_path, monkeypatch, line):
    pkg = _tree(tmp_path, "connectors/x.py", line + "\n", monkeypatch)
    assert _violations(pkg) + _file_rule_violations(pkg), line


def test_connectors_allowed_files_pass_the_scan(tmp_path, monkeypatch):
    pkg = _tree(tmp_path, "connectors/manager.py", "import socket\nfrom chimericmesh.services import registry\n",
                monkeypatch)
    (pkg / "services" / "registry.py").write_text("")
    (pkg / "connectors" / "tailread.py").write_text("import os\nimport asyncio\nfrom chimericmesh.store import x\n")
    (pkg / "connectors" / "__init__.py").write_text("from .manager import ConnectorManager\n")
    assert _violations(pkg) + _file_rule_violations(pkg) == []


def test_parser_file_importing_os_fails_the_scan(tmp_path, monkeypatch):
    pkg = _tree(tmp_path, "parsers/x.py", "import os\n", monkeypatch)
    assert _file_rule_violations(pkg) == ["parsers/x.py imports os"]
    (pkg / "parsers" / "x.py").write_text("import re\n")
    assert _file_rule_violations(pkg) == []
