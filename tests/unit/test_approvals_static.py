"""Static checks of auto-approval (task 8.2): the agent has no path to rules, time and SQL hygiene."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import ast
import re
from pathlib import Path

from test_dependency_direction import group_of
from test_store_static import sql_violations

SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"
APPROVALS = SRC / "approvals"

ALLOWED_MCP_PREFIXES = ("/api/incidents", "/api/events", "/api/irc/say", "/api/proposals")
FORBIDDEN_MCP_PREFIXES = ("/api/rules", "/api/approvals", "/api/audit")
FORBIDDEN_MCP_SUFFIXES = ("/rule", "/rule-defaults", "/withdraw")


def _approvals_files() -> list[Path]:
    return sorted(p for p in APPROVALS.glob("*.py"))


def _mcp_paths() -> list[str]:
    tree = ast.parse((SRC / "mcp_server.py").read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_call" \
                and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                parts = [v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values]
                found.append("".join(parts))
    return found


def test_mcp_endpoints_are_on_the_allow_list_and_never_reach_rules():
    paths = _mcp_paths()
    assert paths, "the scan found no _call sites"
    for path in paths:
        assert path.startswith(ALLOWED_MCP_PREFIXES), path
        assert not path.startswith(FORBIDDEN_MCP_PREFIXES), path
        assert not path.rstrip("}").endswith(FORBIDDEN_MCP_SUFFIXES), path
        assert not re.search(r"/rules?\b|/rule-defaults|/withdraw|/approvals|/audit", path), path


def test_mcp_tool_names_mention_no_rules_or_approvals():
    tree = ast.parse((SRC / "mcp_server.py").read_text(encoding="utf-8"))
    tools = [n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
             and any("tool" in ast.unparse(d) for d in n.decorator_list)]
    assert "propose_remediation" in tools and "complete_proposal" in tools
    for name in tools:
        if name in ("propose_remediation", "complete_proposal"):
            continue
        assert "rule" not in name and "approv" not in name and "pause" not in name and "revoke" not in name, name


def test_the_scanner_would_catch_a_rule_endpoint(tmp_path):
    bad = "async def t():\n    return await _call('POST', '/api/rules', json={})\n"
    tree = ast.parse(bad)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_call")
    assert call.args[1].value.startswith(FORBIDDEN_MCP_PREFIXES)


def test_nothing_in_approvals_reads_the_wall_clock():
    for path in _approvals_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert not (node.value.id == "time" and node.attr in ("time", "monotonic_ns", "time_ns")), \
                    f"{path.name}:{node.lineno} reads time.{node.attr}"
                assert not (node.value.id == "datetime" and node.attr in ("now", "utcnow", "today")), path.name
            if isinstance(node, ast.Attribute) and node.attr in ("utcnow", "today") and path.name != "routes.py":
                raise AssertionError(f"{path.name}:{node.lineno}")


def test_hub_transition_is_called_only_from_the_manager_and_the_hub():
    callers = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "hub_transition":
                callers.add(path.relative_to(SRC).as_posix())
    assert callers <= {"approvals/manager.py", "hub.py"}, callers
    assert "approvals/manager.py" in callers


def test_hub_authority_is_built_only_in_hub_py_under_src():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HubAuthority" \
                    and path.name != "hub.py":
                offenders.append(path.name)
    assert offenders == []


def test_every_approvals_file_has_a_group_row_and_the_web_stack_stays_in_routes():
    for path in _approvals_files():
        rel = path.relative_to(SRC).as_posix()
        assert group_of(rel) == "auto-approval", rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        web = imported & {"fastapi", "starlette", "pydantic"}
        assert not web or path.name == "routes.py", (rel, web)
    init = ast.parse((APPROVALS / "__init__.py").read_text(encoding="utf-8"))
    mods = [n.module for n in ast.walk(init) if isinstance(n, ast.ImportFrom)]
    assert not any(m and m.split(".")[-1] == "routes" for m in mods)


def test_no_sql_in_approvals_is_built_from_data():
    problems = []
    for path in _approvals_files():
        bad, _ = sql_violations(path.read_text(encoding="utf-8"), path.name)
        problems += bad
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = ast.unparse(node)[2:].upper()
                assert not text.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "PRAGMA ", "CREATE ")), \
                    f"{path.name}:{node.lineno}"
    assert problems == []
