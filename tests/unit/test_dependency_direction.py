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
        for top, parts in imports_of(path):
            if top != PKG and top not in stdlib:
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
