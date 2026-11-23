"""Route table and exact tool set (task 8.2, R1.1, R1.8, R2.4, Success Criterion 6)."""
from __future__ import annotations
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import ast
import asyncio
import inspect
import os
import re
import tempfile
from pathlib import Path
import pytest
from chimericmesh import mcp_server
from chimericmesh.api import create_app
from chimericmesh.hub import Hub
from route_owners import OWNERS
SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"
BASELINE_TOOLS = {"list_incidents", "get_incident", "find_code_for_incident", "recent_events", "announce_to_irc",
                  "resolve_incident", "propose_remediation", "get_proposal", "list_proposals", "complete_proposal"}
NEW_TOOLS = {"list_services", "get_service", "service_events", "find_code_for_service", "verify_proposal",
             "wait_for_work", "explain_log_window"}
FORBIDDEN_WORDS = ("rule", "approval", "token", "discovery", "audit", "withdraw", "pause", "resume", "decide",
                   "create_service", "update_service", "delete_service", "delete", "patch", "admin")
@pytest.fixture(scope="module")
def app():
    tmp = tempfile.TemporaryDirectory()
    old = os.environ.get("CHIMERICMESH_HOME")
    os.environ["CHIMERICMESH_HOME"] = os.path.join(tmp.name, "home")
    try:
        yield create_app(Hub(), start_listeners=False)
    finally:
        if old is None:
            os.environ.pop("CHIMERICMESH_HOME", None)
        else:
            os.environ["CHIMERICMESH_HOME"] = old
        tmp.cleanup()
def tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
def app_routes(app) -> set[tuple[str, str]]:
    return {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ())}
def test_the_exact_tool_set_is_the_baseline_plus_seven():
    names = tool_names()
    assert names == BASELINE_TOOLS | NEW_TOOLS and len(names) == 17
def test_no_tool_name_reaches_rules_approvals_services_mutators_tokens_discovery_or_audit():
    allowed_with_word = {"propose_remediation", "complete_proposal", "verify_proposal"}   # proposals are not rules
    for name in tool_names():
        if name in allowed_with_word:
            continue
        for word in FORBIDDEN_WORDS:
            assert word not in name, (name, word)
    for name in tool_names():
        assert "service" not in name or name in {"list_services", "get_service", "service_events",
                                                "find_code_for_service"}, name
def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse((SRC / "mcp_server.py").read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
            and any("tool" in ast.unparse(d) for d in n.decorator_list)}
def _route_literal(node: ast.AST) -> tuple[str, str]:
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "_call"]
    assert len(calls) == 1, ast.unparse(node)[:80]
    method, path = calls[0].args[0], calls[0].args[1]
    assert isinstance(method, ast.Constant)
    if isinstance(path, ast.Constant):
        text = path.value
    else:
        assert isinstance(path, ast.JoinedStr)
        text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in path.values)
    return method.value, text
def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)
def test_each_tool_maps_to_exactly_one_route_that_exists_in_the_app(app):
    routes = {(m, _shape(p)) for m, p in app_routes(app)}
    functions = _tool_functions()
    assert set(functions) == tool_names()
    for name, node in functions.items():
        method, path = _route_literal(node)
        assert (method, _shape(path)) in routes, (name, method, path)
def test_every_route_of_the_app_has_an_owner(app):
    missing = sorted(route for route in app_routes(app) if route not in OWNERS)
    assert missing == [], f"add these routes to tests/unit/route_owners.py: {missing}"
    stale = sorted(route for route in OWNERS if route not in app_routes(app))
    assert stale == [], stale
def test_the_agent_owner_has_exactly_the_six_routes():
    mine = {route for route, owner in OWNERS.items() if owner == "agent-loop"}
    assert len(mine) == 6
    assert {m for m, _ in mine} == {"GET", "POST"}
def test_tools_only_reach_routes_owned_by_the_allowed_modules(app):
    allowed_owners = {"baseline", "services", "auto-approval", "agent-loop"}
    shapes = {}
    for route, owner in OWNERS.items():
        shapes[(route[0], _shape(route[1]))] = (route, owner)
    for name, node in _tool_functions().items():
        method, path = _route_literal(node)
        route, owner = shapes[(method, _shape(path))]
        assert owner in allowed_owners, (name, route, owner)
        if owner == "services":
            assert method == "GET", (name, route)                 # a tool may only read the service registry
        if owner == "auto-approval":
            assert route in {("GET", "/api/proposals"), ("POST", "/api/proposals"), ("GET", "/api/proposals/{pid}"),
                             ("POST", "/api/proposals/{pid}/complete")}, (name, route)
        assert owner != "discovery"
def test_no_route_behind_a_tool_depends_on_require_admin(app):
    shapes = {(m, _shape(p)) for _, node in _tool_functions().items() for m, p in [_route_literal(node)]}
    checked = 0
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            if (method, _shape(route.path)) not in shapes:
                continue
            checked += 1
            for dep in route.dependant.dependencies:
                call = dep.call
                label = f"{getattr(call, '__name__', '')} {getattr(call, '__qualname__', '')}"
                assert "require_admin" not in label, (method, route.path)
    assert checked >= len(shapes)
def test_admin_routes_are_not_reachable_through_a_tool(app):
    admin = {(m, _shape(r.path)) for r in app.routes for m in (getattr(r, "methods", None) or ())
             if any("require_admin" in f"{getattr(d.call, '__name__', '')} {getattr(d.call, '__qualname__', '')}"
                    for d in getattr(getattr(r, "dependant", None), "dependencies", ()))}
    reachable = {(m, _shape(p)) for _, node in _tool_functions().items() for m, p in [_route_literal(node)]}
    assert admin and not (admin & reachable)
def test_no_tool_takes_a_token_parameter():
    for name, node in _tool_functions().items():
        params = [a.arg for a in node.args.args + node.args.kwonlyargs]
        for bad in ("token", "admin_token", "authorization", "password", "pass"):
            assert bad not in params, (name, params)
def test_tool_defaults_are_immutable_and_wait_for_work_signature():
    sig = inspect.signature(mcp_server.wait_for_work)
    assert list(sig.parameters) == ["timeout_seconds", "service_id", "exclude_ids"]
    assert sig.parameters["exclude_ids"].default is None and sig.parameters["timeout_seconds"].default == 25
