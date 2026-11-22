"""Environment placeholders and wildcard expansion of catalog paths (R2.2, R3.2, R3.6). Pure: directory
listing is injected as ``lister``. The path syntax follows ``os_name``, not the host."""
from __future__ import annotations

import ntpath
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .models import MAX_NAMES_PER_PATTERN
from .text import CONTROL

ALLOWED_ENV: dict[str, tuple[str, ...]] = {
    "windows": ("ProgramData", "SystemDrive", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)"),
    "linux": ("HOME",),
    "macos": ("HOME",),
}
Lister = Callable[[str, int], tuple[list[str], bool]]
_POSIX_NAME = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=/|$)")
_WIN_NAME = re.compile(r"%([^%\\/]{1,40})%")
_DRIVE_ONLY = re.compile(r"[A-Za-z]:")
_WIN_ABS = re.compile(r"[A-Za-z]:[\\/]")
_WIN_UNC = re.compile(r"[\\/]{2}[^\\/]+[\\/][^\\/]+")


@dataclass(frozen=True, slots=True)
class ExpandResult:
    paths: tuple[str, ...]
    truncated: bool


_EMPTY = ExpandResult((), False)


def _lookup(env: Mapping[str, str], name: str, os_name: str) -> str | None:
    """Read one allow-listed name (``os.environ`` keys are upper case on Windows)."""
    tries = (name, name.upper()) if os_name == "windows" else (name,)
    for key in dict.fromkeys(tries):
        try:
            value = env.get(key)
        except Exception:  # noqa: BLE001 - a hostile mapping must not break a scan
            return None
        if value is not None:
            return value if isinstance(value, str) else None
    return None


def _clean_base(value: str, os_name: str, name: str = "") -> str | None:
    if not value or CONTROL.search(value) or len(value) > 512:
        return None
    if os_name == "windows":
        if _DRIVE_ONLY.fullmatch(value):                 # "C:" is the normal SystemDrive value
            return value + "\\" if name.casefold() == "systemdrive" else None
        if not (_WIN_ABS.match(value) or _WIN_UNC.fullmatch(ntpath.splitdrive(value)[0])):
            return None                                  # a drive path or a UNC server and share
        stripped = value.rstrip("\\/")
        return stripped if stripped and not _DRIVE_ONLY.fullmatch(stripped) else value
    if not posixpath.isabs(value):
        return None
    return value.rstrip("/") or "/"


def expand_leading_env(pattern: str, os_name: str, env: Mapping[str, str]) -> tuple[str, str] | None:
    """``(base, rest)`` when ``pattern`` starts with an allow-listed placeholder whose value is a clean
    absolute path; ``rest`` is the pattern text after the placeholder, unchanged. Else ``None``."""
    allowed = ALLOWED_ENV.get(os_name)
    if allowed is None or not isinstance(pattern, str):
        return None
    if os_name == "windows":
        match = _WIN_NAME.match(pattern)
    else:
        match = _POSIX_NAME.match(pattern)
    if match is None:
        return None
    name = next((a for a in allowed if a.casefold() == match.group(1).casefold()), None)
    if name is None or (os_name != "windows" and match.group(1) != name):
        return None
    value = _lookup(env, name, os_name)
    if value is None:
        return None
    base = _clean_base(value, os_name, name)
    if base is None:
        return None
    return base, pattern[match.end():]


def _component_regex(component: str, os_name: str) -> re.Pattern[str]:
    body = "".join("[^\\\\/]*" if ch == "*" else "[^\\\\/]" if ch == "?" else re.escape(ch)
                   for ch in component)
    return re.compile(body, re.IGNORECASE if os_name == "windows" else 0)


def _split_literal(pattern: str, os_name: str) -> tuple[str, str] | None:
    """A pattern without a placeholder must be absolute and hold no placeholder character."""
    if os_name == "windows":
        if "%" in pattern or not _WIN_ABS.match(pattern):
            return None
        return pattern[:2] + "\\", pattern[3:]
    if "$" in pattern or not pattern.startswith("/"):
        return None
    return "/", pattern[1:]


def expand_pattern(pattern: str, os_name: str, env: Mapping[str, str], lister: Lister,
                   *, max_names: int = MAX_NAMES_PER_PATTERN) -> ExpandResult:
    """Concrete candidate paths for ``pattern`` (not a check that they exist)."""
    if os_name not in ALLOWED_ENV or not isinstance(pattern, str) or CONTROL.search(pattern):
        return _EMPTY
    led = expand_leading_env(pattern, os_name, env)
    if led is None:
        if pattern[:1] in ("%", "$"):
            return _EMPTY
        led = _split_literal(pattern, os_name)
        if led is None:
            return _EMPTY
    base, rest = led
    if "%" in rest and os_name == "windows" or "$" in rest and os_name != "windows":
        return _EMPTY                                   # a placeholder anywhere but the start
    components = [c for c in re.split(r"[\\/]" if os_name == "windows" else "/", rest) if c]
    if any(c in (".", "..") for c in components):
        return _EMPTY
    join = ntpath.join if os_name == "windows" else posixpath.join
    current = [base]
    listed = 0
    truncated = False
    for component in components:
        if "*" not in component and "?" not in component:
            current = [join(c, component) for c in current]
            continue
        regex = _component_regex(component, os_name)
        nxt: list[str] = []
        for directory in current:
            budget = max_names - listed
            if budget <= 0:
                truncated = True
                break
            try:
                names, more = lister(directory, budget)
            except Exception:  # noqa: BLE001 - an unreadable directory yields no names
                continue
            names = list(names)[:budget]
            listed += len(names)
            truncated = truncated or bool(more)
            for name in sorted(n for n in names if isinstance(n, str)):
                if not name or CONTROL.search(name) or "/" in name or (os_name == "windows" and "\\" in name):
                    continue
                if regex.fullmatch(name):
                    nxt.append(join(directory, name))
        current = nxt
    return ExpandResult(tuple(sorted(dict.fromkeys(current))), truncated)
