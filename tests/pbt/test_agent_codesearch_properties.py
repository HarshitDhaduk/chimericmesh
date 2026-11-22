"""Property 3 of agent-loop: code search is contained and bounded (R1.4)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from chimericmesh.agent.codesearch import search_code  # noqa: E402
from chimericmesh.approvals.scope import PROTECTED_PATTERNS, SOURCE_SUFFIXES, glob_to_regex  # noqa: E402
from chimericmesh.diagnostics import SKIP_DIRS  # noqa: E402

FOLDER = os.path.join(os.path.abspath(os.sep), "proj")
OUTSIDE = os.path.join(os.path.abspath(os.sep), "elsewhere")
PROT = [glob_to_regex(p) for p in PROTECTED_PATTERNS]
CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f]")

DIRS = sorted(SKIP_DIRS) + [".kiro", ".github", "src", "lib", "data", "a", "b"]
FILES = ["a.py", "b.py", "c.php", "d.cs", "e.json", "f.ini", "g.txt", ".env", ".env.local", "k.pem", "s.key",
         "id_rsa", "h.js", "link_x.py", "link_y.go", "LICENSE", "big.py"]
LINES = ["needle here", "NEEDLE upper", "a.*b", "x(y", "[abc]+", "  indented hit  ", "plain", "x" * 300 + " needle",
         "tab\there needle", "ctrl\x1b\x00 needle", "caf\u00e9 needle", ""]

paths = st.builds(lambda d, f: "/".join(d + [f]), st.lists(st.sampled_from(DIRS), max_size=3), st.sampled_from(FILES))
trees = st.dictionaries(paths, st.lists(st.sampled_from(LINES), max_size=6).map("\n".join), max_size=40)
queries = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cs")), min_size=1, max_size=100),
    st.text(alphabet=".*+?[](){}|^$\\a b", min_size=1, max_size=20),
    st.sampled_from(["needle", "NEEDLE", ".*", "a(b", "x(y", "[abc]+", "a.*b"]),
)


def make_walk(tree: dict[str, str]):
    dirs: dict[str, set[str]] = {"": set()}
    files: dict[str, list[str]] = {"": []}
    for rel in tree:
        parts = rel.split("/")
        for i in range(len(parts) - 1):
            parent, child = "/".join(parts[:i]), "/".join(parts[: i + 1])
            dirs.setdefault(parent, set()).add(parts[i])
            dirs.setdefault(child, set())
            files.setdefault(child, [])
        files.setdefault("/".join(parts[:-1]), []).append(parts[-1])

    def walk(top, followlinks=False):
        def rec(rel):
            dirnames = sorted(dirs.get(rel, ()))
            names = files.get(rel, [])
            dirpath = top if not rel else os.path.join(top, *rel.split("/"))
            yield dirpath, dirnames, names
            for d in list(dirnames):               # the caller may prune ``dirnames`` in place
                yield from rec(d if not rel else f"{rel}/{d}")
        yield from rec("")
    return walk


def fake_realpath(path: str) -> str:
    return os.path.join(OUTSIDE, os.path.basename(path)) if os.path.basename(path).startswith("link_") else path


def oracle(tree: dict[str, str], query: str):
    """Independent traversal: nested dicts, files before sub-directories, both sorted."""
    needle = query.casefold()
    root: dict = {}
    for rel in tree:
        node = root
        *dirs_, leaf = rel.split("/")
        for d in dirs_:
            node = node.setdefault(d, {})
        node[leaf] = rel                                    # a str marks a file
    hits: list[tuple[str, int]] = []

    def visit(node: dict, trail: list[str]) -> None:
        for name in sorted(k for k, v in node.items() if isinstance(v, str)):
            rel = node[name]
            if any(rx.match(rel) for rx in PROT):
                continue
            if os.path.splitext(name)[1].lower() not in SOURCE_SUFFIXES or name.startswith("link_"):
                continue
            for n, line in enumerate(tree[rel].split("\n"), start=1):
                if needle in line.casefold():
                    hits.append((rel, n))
        for name in sorted(k for k, v in node.items() if isinstance(v, dict)):
            sub = trail + [name]
            if name in SKIP_DIRS or any(rx.match("/".join(sub) + "/_") for rx in PROT):
                continue
            visit(node[name], sub)
    visit(root, [])
    return hits

def run(tree, query, *, clock=lambda: 0.0, reads=None):
    def read(path):
        if reads is not None:
            reads.append(path)
        rel = os.path.relpath(path, FOLDER).replace(os.sep, "/")
        return tree.get(rel)
    return search_code(FOLDER, query, walk=make_walk(tree), getsize=lambda p: 10, read_text=read,
                       realpath=fake_realpath, normcase=lambda s: s, clock=clock)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trees, queries)
def test_property_3_hits_are_contained_clean_and_bounded(tree, query):
    reads: list[str] = []
    result = run(tree, query, reads=reads)
    assert len(result.hits) <= 10
    assert len(reads) <= 2000
    for hit in result.hits:
        parts = hit.file.split("/")
        assert not hit.file.startswith(("/", "\\")) and ".." not in parts and ":" not in hit.file
        assert hit.file in tree                                         # inside the folder
        assert len(hit.file) <= 300 and not CONTROL.search(hit.file)
        assert not (set(parts[:-1]) & SKIP_DIRS)
        assert not any(rx.match(hit.file) for rx in PROT)
        assert os.path.splitext(parts[-1])[1].lower() in SOURCE_SUFFIXES
        assert not parts[-1].startswith("link_")                        # resolved outside via realpath
        assert len(hit.text) <= 200 and not CONTROL.search(hit.text)
        assert hit.line >= 1


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trees, queries)
def test_property_3_a_complete_search_equals_the_independent_oracle(tree, query):
    result = run(tree, query)
    expected = oracle(tree, query)
    got = [(h.file, h.line) for h in result.hits]
    if result.truncated:
        assert len(result.hits) == 10 and got == expected[:10] and len(expected) > 10
    else:
        assert got == expected                                          # literal text only, nothing else


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet=".*+?[](){}|^$\\", min_size=1, max_size=12))
def test_property_3_a_regex_query_matches_only_the_literal_text(query):
    tree = {"lit.py": f"before {query} after", "other.py": "abc abc abc 123 [x] (y) a.b", "none.py": ""}
    result = run(tree, query)
    assert {h.file for h in result.hits} == {f for f, text in tree.items() if query.casefold() in text.casefold()}


@settings(max_examples=60, deadline=None)
@given(st.floats(min_value=0.1, max_value=3.0), st.lists(st.sampled_from(["a.py", "b.py", "c.py"]), min_size=1, max_size=3))
def test_property_3_the_clock_stops_the_search_and_sets_truncated(step, names):
    tree = {f"d{n}/{name}": "needle\n" for n in range(60) for name in names}
    now = [0.0]
    reads_at: list[float] = []

    def clock():
        now[0] += step
        return now[0]

    def read(path):
        reads_at.append(now[0])
        return "needle\n"
    result = search_code(FOLDER, "needle", walk=make_walk(tree), getsize=lambda p: 1, read_text=read,
                         realpath=lambda p: p, normcase=lambda s: s, clock=clock)
    assert all(t - step <= 5.0 for t in reads_at)           # no read after the clock passed 5 s
    total = len(tree)
    if len(reads_at) < min(total, 10):
        assert result.truncated
    if len(reads_at) < total and len(result.hits) < 10:
        assert result.truncated is True
