"""Static-check helpers for src/chimericmesh/web/index.html (dashboard task 1.3).

The page is one HTML file with one inline script. There is no JavaScript runner, so the rules of the
design are checked from Python: a small tokenizer separates code, strings, regex literals and comments.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "web" / "index.html"

SECTIONS = ["PURE", "SELFTEST", "DOM helpers", "API", "STATE", "SCHEDULER", "VIEWS", "INIT"]

# sha256 of the text of `function name(...) { ... }` in the page of main at 3a12345, before any page task
# (open item O18). Task 7.4 compares the page against these.
BASELINE_HASHES = {
    "renderGuide": "8734a5d754c4fcfd299663a4e92551235af1e81ae3f96eab1e9536b680266628",
    "doIt": "4983c5851a0bd596465ee309676d384872edae7b25a08692974704652318260f",
}


@lru_cache(maxsize=1)
def load_page() -> str:
    return PAGE.read_text(encoding="utf-8")


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.ids: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        self.tags.append((tag, d))
        if d.get("id"):
            self.ids.append(d["id"])

    handle_startendtag = handle_starttag


def html_parser() -> _Collector:
    p = _Collector()
    p.feed(load_page())
    p.close()
    return p


def script_text() -> str:
    """The inline script(s), concatenated (the page has one)."""
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", load_page(), re.S))


_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}


def scan(text: str):
    """Yield (kind, chunk) with kind in code, string, regex, comment. Heuristic regex detection."""
    i, n = 0, len(text)
    code_start = 0
    prev = ""  # previous significant code character
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            if code_start < i:
                yield "code", text[code_start:i]
            j = text.find("\n", i)
            j = n if j < 0 else j
            yield "comment", text[i:j]
            i = code_start = j
        elif c == "/" and nxt == "*":
            if code_start < i:
                yield "code", text[code_start:i]
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            yield "comment", text[i:j]
            i = code_start = j
        elif c in "'\"`":
            if code_start < i:
                yield "code", text[code_start:i]
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            yield "string", text[i:j]
            i = code_start = j
            prev = "s"
        elif c == "/" and prev in _REGEX_PREV:
            if code_start < i:
                yield "code", text[code_start:i]
            j, in_class = i + 1, False
            while j < n and (in_class or text[j] != "/") and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                j += 1
            j = min(j + 1, n)
            while j < n and text[j].isalpha():
                j += 1
            yield "regex", text[i:j]
            i = code_start = j
            prev = "r"
        else:
            if not c.isspace():
                prev = c
            i += 1
    if code_start < n:
        yield "code", text[code_start:]


@lru_cache(maxsize=None)
def strip_comments(text: str) -> str:
    """Remove // and /* */ comments; strings and regex literals are kept as they are."""
    out = []
    for kind, chunk in scan(text):
        out.append("\n" if kind == "comment" and chunk.startswith("//") else " " if kind == "comment" else chunk)
    return "".join(out)


@lru_cache(maxsize=None)
def code_only(text: str) -> str:
    """Comments removed and string/regex bodies blanked: what is left is the code structure."""
    out = []
    for kind, chunk in scan(text):
        out.append(chunk if kind == "code" else "''" if kind in ("string", "regex") else " ")
    return "".join(out)


def section(name: str) -> str:
    """Script text between the banner `/* ==== NAME ==== */` and the next banner (or the end)."""
    s = script_text()
    banner = f"/* ==== {name} ==== */"
    start = s.find(banner)
    if start < 0:
        raise KeyError(name)
    start += len(banner)
    m = re.compile(r"/\* ==== [^=]+? ==== \*/").search(s, start)
    return s[start : m.start() if m else len(s)]


@lru_cache(maxsize=None)
def functions(text: str) -> dict[str, str]:
    """Top-level `function name(` (also `async function`) bodies, by name, with brace matching."""
    found: dict[str, str] = {}
    flat = code_only(text)  # same length caveat: strings blanked, so use positions in `text` via scan
    # Rebuild a brace-depth map over the original text using the tokenizer positions.
    depth_at: list[int] = []
    depth = 0
    pos = 0
    for kind, chunk in scan(text):
        if kind == "code":
            for ch in chunk:
                if ch == "{":
                    depth += 1
                depth_at.append(depth)
                if ch == "}":
                    depth -= 1
        else:
            depth_at.extend([depth] * len(chunk))
        pos += len(chunk)
    del flat
    for m in re.finditer(r"(?:^|(?<=\n)|(?<=;)|(?<=\s))(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", text):
        if depth_at[m.start()] != 0:
            continue
        i = text.index("{", m.end())
        # brace-match from i using the tokenizer so strings with braces do not count
        d, end = 0, None
        offset = 0
        for kind, chunk in scan(text):
            if offset + len(chunk) <= i:
                offset += len(chunk)
                continue
            if kind == "code":
                for k, ch in enumerate(chunk):
                    if offset + k < i:
                        continue
                    if ch == "{":
                        d += 1
                    elif ch == "}":
                        d -= 1
                        if d == 0:
                            end = offset + k + 1
                            break
            if end is not None:
                break
            offset += len(chunk)
        assert end is not None, m.group(1)
        start = text.index("function", m.start())
        found[m.group(1)] = text[start:end]
    return found


def function_hash(name: str) -> str:
    return hashlib.sha256(functions(script_text())[name].encode("utf-8")).hexdigest()
