"""Task 1.6: the format registry and the static safety scan."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from chimericmesh.parsers import formats
from chimericmesh.parsers.formats import FORMAT_NAMES, FORMATS, get_format
from chimericmesh.parsers.logctx import FormatContext, LineFormat

SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"
PARSER_FILES = ["logctx.py", "plain.py", "syslogtext.py", "ncsa.py", "w3c.py", "xferlog.py", "formats.py"]
BAD_CALLS = {"eval", "exec", "compile", "__import__"}
BAD_IMPORTS = {"subprocess", "importlib", "pickle", "marshal"}


def test_format_names_are_exactly_the_five():
    assert FORMAT_NAMES == ("syslog-text", "ncsa", "w3c", "xferlog", "plain")
    assert set(FORMATS) == set(FORMAT_NAMES)


def test_format_names_equal_the_services_default():
    from chimericmesh.services.validate import DEFAULT_FORMATS

    assert set(DEFAULT_FORMATS) == set(FORMAT_NAMES)


def test_unknown_format_raises_key_error():
    with pytest.raises(KeyError):
        get_format("nope")


@pytest.mark.parametrize("name", FORMAT_NAMES)
def test_parse_equals_parse_line_event(name):
    fmt = get_format(name)
    ctx = FormatContext(machine="m", service_id="s")
    for line in ("hello error", "", "Nov 18 10:00:01 h app: x"):
        a = fmt.parse(line, ctx)
        b = fmt.parse_line(line, ctx).event
        assert (a is None) == (b is None)
        if a is not None:
            assert (a.host, a.app, a.severity, a.message, a.raw) == (b.host, b.app, b.severity, b.message, b.raw)


def test_update_is_identity_except_for_w3c():
    ctx = FormatContext(machine="m")
    for name in FORMAT_NAMES:
        out = get_format(name).update("#Fields: a b sc-status", ctx)
        if name == "w3c":
            assert out.w3c_fields == ("a", "b", "sc-status")
        else:
            assert out is ctx


@pytest.mark.parametrize("name", FORMAT_NAMES)
def test_parse_signature(name):
    fmt = get_format(name)
    assert isinstance(fmt, LineFormat)
    params = list(inspect.signature(fmt.parse).parameters)
    assert params == ["line", "ctx"] or params == ["line", "context"]


def _scan(tree: ast.AST) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BAD_CALLS:
                problems.append(f"call {f.id}")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if f.value.id == "builtins" and f.attr in BAD_CALLS:
                    problems.append(f"call builtins.{f.attr}")
                if f.value.id == "os" and f.attr in {"system", "popen"}:
                    problems.append(f"call os.{f.attr}")
        elif isinstance(node, ast.Import):
            problems += [f"import {a.name}" for a in node.names if a.name.split(".")[0] in BAD_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BAD_IMPORTS:
                problems.append(f"import {node.module}")
    return problems


def test_scanner_flags_the_dangerous_forms_and_allows_re_compile():
    bad = ast.parse("eval('1')\nexec('x')\ncompile('x','f','exec')\n__import__('os')\nimport builtins\n"
                    "builtins.eval('1')\nimport os\nos.system('x')\nos.popen('x')\nimport subprocess\n"
                    "from importlib import import_module\nimport pickle\nimport marshal\n")
    assert len(_scan(bad)) == 11
    assert _scan(ast.parse("import re\nX = re.compile('a')\n")) == []


def test_parser_and_connector_files_pass_the_scan():
    files = [SRC / "parsers" / n for n in PARSER_FILES]
    connectors = SRC / "connectors"
    if connectors.is_dir():
        files += sorted(connectors.glob("*.py"))
    problems = {str(p.relative_to(SRC)): _scan(ast.parse(p.read_text(encoding="utf-8"))) for p in files}
    assert {k: v for k, v in problems.items() if v} == {}


def test_parser_files_import_only_events_syslog_and_each_other():
    allowed = {"events", "syslog", "logctx", "plain", "syslogtext", "ncsa", "w3c", "xferlog", "formats"}
    for name in PARSER_FILES:
        tree = ast.parse((SRC / "parsers" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("chimericmesh")):
                mod = (node.module or "").split(".")[-1]
                if node.level == 1 and not node.module:
                    continue
                assert mod in allowed, (name, node.module)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("chimericmesh") for a in node.names), name


def test_module_exports():
    assert formats.FORMAT_NAMES == tuple(FORMATS)
