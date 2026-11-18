"""Static guards: no SQL built from data, no plain-def routes, HubAuthority built only in hub.py."""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"
STORE = SRC / "store"
SQL_METHODS = {"execute", "executemany", "one", "all"}

# The one function allowed to build SQL text (PRAGMA user_version cannot take a parameter).
DYNAMIC_EXEMPT = {("migrations.py", "_set_user_version")}
# Functions that pass a bare name through (B1, option A). Exact list: a new one fails test_pass_through_list_is_exact.
PASS_THROUGH = {
    ("tx.py", "execute"), ("tx.py", "executemany"), ("tx.py", "one"), ("tx.py", "all"),
    ("tx.py", "savepoint"), ("migrations.py", "_run"),
}
# Names those functions may pass: forwarded parameters, the _SAVEPOINTS bindings, the m.apply loop variable.
PASS_THROUGH_NAMES = {"sql", "begin", "release", "rollback", "statement"}


def module_constants(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def sql_violations(source: str, filename: str = "scratch.py") -> tuple[list[str], set[tuple[str, str]]]:
    """Return (violations, functions that used a pass-through or exempt construction)."""
    tree = ast.parse(source)
    consts = module_constants(tree)
    bad: list[str] = []
    used: set[tuple[str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in SQL_METHODS and node.args:
                arg, fn = node.args[0], (self.stack[-1] if self.stack else "<module>")
                key = (filename, fn)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    pass
                elif isinstance(arg, ast.Name) and arg.id in consts:
                    pass
                elif key in DYNAMIC_EXEMPT:
                    used.add(key)
                elif isinstance(arg, ast.Name) and key in PASS_THROUGH and arg.id in PASS_THROUGH_NAMES:
                    used.add(key)
                else:
                    bad.append(f"{filename}:{node.lineno} in {fn}: {ast.unparse(arg)[:70]}")
            self.generic_visit(node)

    Visitor().visit(tree)
    return bad, used


def store_files():
    return sorted(STORE.glob("*.py"))


def test_no_sql_is_built_from_data():
    problems = []
    for path in store_files():
        bad, _ = sql_violations(path.read_text(encoding="utf-8"), path.name)
        problems += bad
    assert problems == []


def test_pass_through_list_is_exact():
    """Every listed function really passes a bare name (no stale entries) and no other function does."""
    used_all: set[tuple[str, str]] = set()
    for path in store_files():
        _, used = sql_violations(path.read_text(encoding="utf-8"), path.name)
        used_all |= used
    assert used_all == PASS_THROUGH | DYNAMIC_EXEMPT


def test_only_set_user_version_builds_dynamic_sql():
    assert DYNAMIC_EXEMPT == {("migrations.py", "_set_user_version")}
    for path in store_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.JoinedStr):
                    text = ast.unparse(node)[2:].upper()  # drop the f and the opening quote
                    if text.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "PRAGMA ", "CREATE ", "DROP ", "ALTER ")):
                        assert fn.name == "_set_user_version", f"{path.name}:{node.lineno}"


def test_the_guard_catches_sql_built_inside_a_forwarding_function():
    scratch = '''
class Tx:
    def execute(self, sql, params=()):
        return self._c.execute(sql, params)
    def helper(self, x):
        return self.execute(f"SELECT * FROM t WHERE a = {x}")
    def concat(self, x):
        return self.one("SELECT " + x)
    def pct(self, x):
        return self.all("SELECT %s" % x)
    def fmt(self, x):
        return self.all("SELECT {}".format(x))
    def name(self, q):
        return self.all(q)
'''
    bad, used = sql_violations(scratch, "tx.py")
    assert len(bad) == 5, bad
    assert ("tx.py", "execute") in used  # the legitimate forward is still recognised


def test_optional_filters_use_the_null_tolerant_pattern():
    text = "\n".join(p.read_text(encoding="utf-8") for p in store_files())
    assert "IS NULL OR" in text
    for line in text.splitlines():
        assert not re.search(r"\bWHERE\b.*\{", line), line  # no string-built WHERE


def test_no_route_is_a_plain_def():
    tree = ast.parse((SRC / "api.py").read_text(encoding="utf-8"))
    plain = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                call = dec.func if isinstance(dec, ast.Call) else dec
                if (isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name)
                        and call.value.id == "app" and call.attr in {"get", "post", "put", "patch", "delete"}):
                    plain.append(node.name)
    assert plain == []


ALLOWED_PREFIXES = ("chimericmesh.diagnostics", "chimericmesh.events", "chimericmesh.parsers",
                    "chimericmesh.fsperms", "chimericmesh.store")


def test_store_imports_only_core_and_store_modules():
    for path in store_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if node.module.startswith("chimericmesh"):
                    assert any(n.startswith(ALLOWED_PREFIXES) for n in names), f"{path.name}: {node.module}"
            elif isinstance(node, ast.ImportFrom) and node.level > 1:
                raise AssertionError(f"{path.name}: relative import leaves the store package")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("chimericmesh") or a.name.startswith(ALLOWED_PREFIXES)


def test_hub_authority_is_constructed_only_in_hub_py():
    """Scans src/ only (tests build it on purpose). The class definition in store/proposals.py is exempt."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HubAuthority"
                    and path.name != "hub.py"):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
    hub_calls = [n for n in ast.walk(ast.parse((SRC / "hub.py").read_text(encoding="utf-8")))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "HubAuthority"]
    assert len(hub_calls) == 1
