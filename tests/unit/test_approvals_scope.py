from __future__ import annotations

import os
from pathlib import Path

import pytest

from chimericmesh import diagnostics
from chimericmesh.approvals.scope import (
    MAX_PATH, PROTECTED_PATTERNS, SOURCE_SUFFIXES, check_file, check_files, glob_to_regex,
)


@pytest.fixture()
def folder(tmp_path: Path) -> str:
    root = tmp_path / "app"
    root.mkdir()
    (tmp_path / "app2").mkdir()
    return str(root)


def code(folder: str, file: str, **kw) -> str | None:
    problem = check_file(folder, file, **kw)
    return None if problem is None else problem.code


def test_source_suffixes_contain_core_code_suffixes():
    assert diagnostics.CODE_SUFFIXES <= SOURCE_SUFFIXES
    assert {".php", ".cs", ".tsx"} <= SOURCE_SUFFIXES


def test_file_inside_folder_passes(folder):
    assert code(folder, os.path.join(folder, "src", "a.py")) is None


def test_relative_path_is_joined_to_the_folder(folder):
    assert code(folder, "upstream.py") is None
    assert code(folder, "sub/dir/x.php") is None


@pytest.mark.parametrize("rel", ["../x.py", "../app2/x.py", "sub/../../x.py"])
def test_escapes_are_outside_folder(folder, rel):
    assert code(folder, rel) == "outside_folder"


def test_absolute_path_outside_and_sibling_with_common_prefix(folder):
    sibling = os.path.join(os.path.dirname(folder), "app2", "x.py")
    assert code(folder, sibling) == "outside_folder"
    assert code(folder, str(Path(folder).parent / "other.py")) == "outside_folder"


def test_the_folder_itself_is_outside(folder):
    assert code(folder, folder) == "outside_folder"


@pytest.mark.parametrize("rel", [
    ".git/hooks/x.py", ".kiro/x.py", ".github/workflows/x.py", "src/.env.py", "a/b/.env.local.py",
    "k.pem", "k.key", "id_rsa", "id_rsa.pub", "c.pfx", ".env",
    "sub/.git/hooks/x.py", "vendor/lib/.kiro/x.py", "pkg/.github/workflows/x.py",
])
def test_protected_paths(folder, rel):
    assert code(folder, rel) == "protected", rel


def test_gitignore_is_not_protected(folder):
    assert code(folder, ".gitignore") != "protected"


def test_protected_dir_entries(folder, tmp_path):
    inner = Path(folder) / "secrets"
    inner.mkdir()
    assert code(folder, "secrets/a.py", protected_dirs=[str(inner)]) == "protected"
    assert code(folder, "other/a.py", protected_dirs=[str(inner)]) is None


@pytest.mark.parametrize("name", ["a.ini", "a.conf", "a.yaml", "a.json", "a.toml", "Makefile", "a.txt"])
def test_config_files_fail_the_extension_rule(folder, name):
    assert code(folder, name) == "extension"


@pytest.mark.parametrize("name", ["a.php", "a.cs", "a.tsx", "A.PY"])
def test_source_files_pass(folder, name):
    assert code(folder, name) is None


def test_too_long_and_control_characters(folder):
    assert code(folder, "a" * (MAX_PATH + 1 - 3) + ".py") == "too_long"
    assert code(folder, "a" * 298 + ".py") == "too_long"
    assert code(folder, "a\x00.py") == "control_char"
    assert code(folder, "a\x1b.py") == "control_char"
    assert code(folder, "a\n.py") == "control_char"


def test_problem_text_is_cleaned_and_cut(folder):
    problem = check_file(folder, "x" * 250 + "\x00.py")
    assert problem is not None and len(problem.file) <= 80
    assert not any(ord(c) < 32 for c in problem.file)


def test_windows_case_folding_with_injected_normcase(folder):
    assert code(folder, "SRC\\A.PY".replace("\\", os.sep), normcase=str.lower) is None
    assert code(folder, ".GIT/X.PY", normcase=str.lower) == "protected"


def test_check_files_keeps_order_and_returns_every_problem(folder):
    found = check_files(folder, ["a.py", "../b.py", "c.json", ".env"])
    assert [(p.file, p.code) for p in found] == [("../b.py", "outside_folder"), ("c.json", "extension"),
                                                 (".env", "protected")]
    assert check_files(folder, []) == []


def test_never_raises_on_odd_input(folder):
    for odd in ["", " ", "\ud800", ":" * 5, "a" * 5000, "CON", "a::$DATA"]:
        check_file(folder, odd)
    assert check_file("", "a.py") is not None


def test_glob_to_regex():
    env = glob_to_regex("**/.env")
    assert env.match(".env") and env.match("a/b/.env") and not env.match("a/.envx")
    git = glob_to_regex("**/.git/**")
    assert git.match(".git/x") and git.match("a/b/.git/x") and not git.match("a/.gitx/y")
    star = glob_to_regex("**/*.pem")
    assert star.match("k.pem") and star.match("a/k.pem") and not star.match("a/k.pem/x")
    assert glob_to_regex("*.py").match("a.py") and not glob_to_regex("*.py").match("d/a.py")


def test_protected_patterns_are_the_documented_set():
    assert PROTECTED_PATTERNS == ("**/.git/**", "**/.kiro/**", "**/.github/**", "**/.env", "**/.env.*",
                                  "**/*.pem", "**/*.key", "**/id_rsa*", "**/*.pfx")
