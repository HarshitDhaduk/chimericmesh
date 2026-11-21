"""File scope of an auto-approved fix: inside the service's code folder, source files only (R5).

Pure and total: ``check_file`` and ``check_files`` never raise. The hub only checks what a proposal
*declares*; it does not stop the agent writing elsewhere (README states this limit, R5.4).
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from chimericmesh.diagnostics import CODE_SUFFIXES
from chimericmesh.services.paths import path_in_code_folder

from .format import safe_text

PROTECTED_PATTERNS = ("**/.git/**", "**/.kiro/**", "**/.github/**", "**/.env", "**/.env.*",
                      "**/*.pem", "**/*.key", "**/id_rsa*", "**/*.pfx")
SOURCE_SUFFIXES = CODE_SUFFIXES | frozenset({".php", ".pl", ".cs", ".cpp", ".cc", ".jsx", ".tsx"})
MAX_PATH = 300
_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")

ProblemCode = Literal["too_long", "control_char", "outside_folder", "protected", "extension"]


@dataclass(frozen=True, slots=True)
class FileProblem:
    file: str            # the declared text, cleaned and cut to 80 characters
    code: ProblemCode


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` is any number of path components, ``*`` stays inside one component."""
    parts = pattern.split("/")
    out = ["^"]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        out.append("[^/]*".join(re.escape(piece) for piece in part.split("*")))
        if not last:
            out.append("/")
    out.append("$")
    return re.compile("".join(out))


_PROTECTED_RES = tuple(glob_to_regex(p) for p in PROTECTED_PATTERNS)


def _problem(file: str, code: ProblemCode) -> FileProblem:
    return FileProblem(safe_text(file, 80), code)


def _inside(path: str, base: str) -> bool:
    try:
        return path != base and os.path.commonpath([path, base]) == base
    except ValueError:
        return False


def check_file(code_folder: str, file: str, *, protected_dirs: Sequence[str] = (),
               realpath: Callable[[str], str] = os.path.realpath,
               normcase: Callable[[str], str] = os.path.normcase) -> FileProblem | None:
    """The first problem of one declared file, or None. Order: length, control, folder, protected, suffix."""
    try:
        if not isinstance(file, str):
            return _problem(str(file), "control_char")
        if len(file) > MAX_PATH:
            return _problem(file, "too_long")
        if _CONTROL.search(file):
            return _problem(file, "control_char")
        if not path_in_code_folder(code_folder, file, realpath=realpath, normcase=normcase):
            return _problem(file, "outside_folder")
        text = file.replace("\\", "/") if os.sep == "/" else file
        joined = text if os.path.isabs(text) else os.path.join(code_folder, text)
        real = normcase(realpath(joined))
        base = normcase(realpath(code_folder))
        rel = os.path.relpath(real, base).replace(os.sep, "/")
        if any(rx.match(rel) for rx in _PROTECTED_RES):
            return _problem(file, "protected")
        for entry in protected_dirs:
            if _inside(real, normcase(realpath(entry))):
                return _problem(file, "protected")
        if os.path.splitext(real)[1].lower() not in SOURCE_SUFFIXES:
            return _problem(file, "extension")
        return None
    except Exception:  # noqa: BLE001 - total by contract: any surprise is a refusal
        return _problem(str(file)[:80], "outside_folder")


def check_files(code_folder: str, files: Sequence[str], *, protected_dirs: Sequence[str] = (),
                realpath: Callable[[str], str] = os.path.realpath,
                normcase: Callable[[str], str] = os.path.normcase) -> list[FileProblem]:
    """Every problem, in the order of ``files``."""
    found: list[FileProblem] = []
    for file in files:
        problem = check_file(code_folder, file, protected_dirs=protected_dirs, realpath=realpath,
                             normcase=normcase)
        if problem is not None:
            found.append(problem)
    return found
