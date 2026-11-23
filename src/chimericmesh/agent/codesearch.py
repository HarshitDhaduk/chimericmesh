"""Literal code search inside one service's code folder (R1.4). Pure over a folder tree, bounded, and total.

Runs in a worker thread (``asyncio.to_thread``) with immutable inputs; it touches no hub object. The query is
text, never a regular expression. Only source files are read (``SOURCE_SUFFIXES``, open item O6), never a
protected file (``PROTECTED_PATTERNS``), never anything that resolves outside the folder. Containment is one ``realpath`` per file (the design note of
2026-11-22: three calls per file made 2000 files exceed 5 s on Windows).
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from chimericmesh.approvals.scope import PROTECTED_PATTERNS, SOURCE_SUFFIXES, glob_to_regex
from chimericmesh.diagnostics import SKIP_DIRS

from .errors import InvalidQuery
from .limits import MAX_FILE_BYTES, MAX_FILES_SCANNED, MAX_HITS, MAX_QUERY, SEARCH_SECONDS, clean_text

HIT_TEXT = 200
MAX_REL_PATH = 300
_PROTECTED = tuple(glob_to_regex(p) for p in PROTECTED_PATTERNS)
_UNSAFE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")


@dataclass(frozen=True, slots=True)
class Hit:
    file: str          # relative to the code folder, "/" separators
    line: int          # 1-based
    text: str          # the stripped line, at most 200 characters, no control characters


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[Hit, ...]
    truncated: bool


def validate_query(query: object) -> str:
    """The query when it is 1 to 100 characters without control characters, else ``InvalidQuery``."""
    if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY or _UNSAFE.search(query):
        raise InvalidQuery("query must be 1 to 100 characters without control characters")
    return query


def _protected(rel: str) -> bool:
    return any(rx.match(rel) for rx in _PROTECTED)


def _within(path: str, base: str) -> bool:
    try:
        return path != base and os.path.commonpath([path, base]) == base
    except ValueError:
        return False


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        return None


def search_code(folder: str, query: str, *, protected_dirs: Sequence[str] = (),
                clock: Callable[[], float] = time.monotonic, cancelled: Callable[[], bool] = lambda: False,
                realpath: Callable[[str], str] = os.path.realpath,
                normcase: Callable[[str], str] = os.path.normcase,
                walk: Callable[..., Iterable[tuple[str, list[str], list[str]]]] = os.walk,
                getsize: Callable[[str], int] = os.path.getsize,
                read_text: Callable[[str], str | None] = _read) -> SearchResult:
    """Up to 10 hits for ``query`` (case-insensitive, literal) under ``folder``.

    ``truncated`` is true when the search stopped early: an eleventh hit, 2000 files visited, 5 seconds on
    ``clock``, or ``cancelled()``. Directories and files are visited in sorted order, so the result is
    deterministic.
    """
    needle = validate_query(query).casefold()
    started = clock()
    hits: list[Hit] = []
    visited = 0
    try:
        base = normcase(realpath(folder))
        prot_dirs = [normcase(realpath(p)) for p in protected_dirs]
    except (OSError, ValueError):
        return SearchResult((), False)

    def stop() -> SearchResult:
        return SearchResult(tuple(hits), True)

    for dirpath, dirnames, filenames in walk(folder, followlinks=False):
        rel_dir = os.path.relpath(dirpath, folder).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not _protected(normcase(f"{prefix}{d}/_")))
        for name in sorted(filenames):
            if visited >= MAX_FILES_SCANNED or cancelled() or clock() - started > SEARCH_SECONDS:
                return stop()
            visited += 1
            path = os.path.join(dirpath, name)
            rel = normcase(f"{prefix}{name}")
            if len(rel) > MAX_REL_PATH or _UNSAFE.search(rel):       # a name that cannot be reported safely
                continue
            if _protected(rel) or os.path.splitext(name)[1].lower() not in SOURCE_SUFFIXES:
                continue
            try:
                if getsize(path) > MAX_FILE_BYTES:
                    continue
            except (OSError, ValueError):
                continue
            try:
                real = normcase(realpath(path))
            except (OSError, ValueError):
                continue
            if not _within(real, base):          # a symlink that leaves the folder
                continue
            real_rel = os.path.relpath(real, base).replace(os.sep, "/")
            if (_protected(real_rel) or os.path.splitext(real)[1].lower() not in SOURCE_SUFFIXES
                    or any(_within(real, d) for d in prot_dirs)):
                continue
            text = read_text(path)
            if text is None:
                continue
            for number, line in enumerate(text.split("\n"), start=1):
                if needle in line.casefold():
                    if len(hits) >= MAX_HITS:
                        return stop()
                    hits.append(Hit(prefix + name, number, clean_text(line.strip(), HIT_TEXT)))
    return SearchResult(tuple(hits), False)
