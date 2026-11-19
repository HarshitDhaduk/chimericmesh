"""Code folder and log path rules. Stat only: no log file is ever opened here."""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Sequence

from .errors import ValidationFailed
from .validate import LIMITS

_DOTDOT = "parent references are not allowed"
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")
_WIN_PREFIX = re.compile(r"^(?:\\\\[?.]\\|//[?.]/)")


def _components(text: str) -> list[str]:
    return [c for c in re.split(r"[\\/]", text) if c]


def _within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lies below it (both already normalized)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_too_broad(resolved: str, *, protected: Sequence[str], home: str | None) -> str | None:
    """None when fine, else the reason: filesystem root, the user's home (or a folder holding it),
    or a folder that equals, contains or lies inside a protected directory."""
    target = _norm(resolved)
    if os.path.dirname(target) == target:
        return "a filesystem root is too broad"
    home_dir = _norm(home if home is not None else os.path.expanduser("~"))
    if _within(home_dir, target):
        return "the home directory (or a folder that contains it) is too broad"
    for item in protected:
        prot = _norm(item)
        if _within(prot, target) or _within(target, prot):
            return "overlaps the hub data directory"
    return None


def normalize_code_folder(raw: str, *, protected: Sequence[str], home: str | None = None) -> str:
    """Absolute, existing directory, no ``..`` component; returns ``os.path.realpath(raw)``."""
    if not isinstance(raw, str) or not raw:
        raise ValidationFailed("code_folder", "must be a non-empty path")
    if "\x00" in raw:
        raise ValidationFailed("code_folder", "must not contain NUL")
    if len(raw) > LIMITS.path:
        raise ValidationFailed("code_folder", f"must be at most {LIMITS.path} characters")
    if ".." in _components(raw):
        raise ValidationFailed("code_folder", _DOTDOT)
    if not os.path.isabs(raw):
        raise ValidationFailed("code_folder", "must be an absolute path")
    try:
        is_dir = os.path.isdir(raw)
    except (OSError, ValueError):
        is_dir = False
    if not is_dir:
        raise ValidationFailed("code_folder", "must be an existing directory")
    resolved = os.path.realpath(raw)
    if len(resolved) > LIMITS.path:
        raise ValidationFailed("code_folder", f"must be at most {LIMITS.path} characters")
    reason = is_too_broad(resolved, protected=protected, home=home)
    if reason is not None:
        raise ValidationFailed("code_folder", reason)
    return resolved


def _windows_suspicious(candidate: str) -> bool:
    """Names Windows rewrites after a check: trailing dot or space, drive-relative, alternate streams."""
    rest = _WIN_PREFIX.sub("", candidate, count=1)
    if _DRIVE_RELATIVE.match(rest):
        return True
    if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
        rest = rest[2:]
    if ":" in rest:
        return True
    for part in re.split(r"[\\/]", rest):
        if part in ("", ".", ".."):
            continue
        if part.endswith((" ", ".")):
            return True
    return False


def path_in_code_folder(code_folder: str, candidate: str, *,
                        realpath: Callable[[str], str] = os.path.realpath,
                        normcase: Callable[[str], str] = os.path.normcase) -> bool:
    """True only when ``candidate`` resolves to a strict descendant of ``code_folder``. Never raises."""
    try:
        if not isinstance(code_folder, str) or not isinstance(candidate, str):
            return False
        if not code_folder or not candidate or "\x00" in code_folder or "\x00" in candidate:
            return False
        if os.name == "nt" and _windows_suspicious(candidate):
            return False
        text = candidate.replace("\\", "/") if os.sep == "/" else candidate
        joined = text if os.path.isabs(text) else os.path.join(code_folder, text)
        real = normcase(realpath(joined))
        base = normcase(realpath(code_folder))
        return real != base and os.path.commonpath([real, base]) == base
    except (OSError, ValueError, TypeError):
        return False


def normalize_log_path(raw: str) -> str:
    """Absolute, no NUL, at most 1024 characters, ``os.path.normpath``. Symlinks are not resolved."""
    if not isinstance(raw, str) or not raw:
        raise ValidationFailed("path", "must be a non-empty path")
    if "\x00" in raw:
        raise ValidationFailed("path", "must not contain NUL")
    if len(raw) > LIMITS.path:
        raise ValidationFailed("path", f"must be at most {LIMITS.path} characters")
    if not os.path.isabs(raw):
        raise ValidationFailed("path", "must be an absolute path")
    out = os.path.normpath(raw)
    if len(out) > LIMITS.path:
        raise ValidationFailed("path", f"must be at most {LIMITS.path} characters")
    return out


def inspect_log_path(path: str) -> None:
    """``os.stat`` only. A missing file and a permission error are accepted; a directory, device,
    pipe or socket is rejected. The file is never opened."""
    try:
        mode = os.stat(path).st_mode
    except (OSError, ValueError):
        return
    if stat.S_ISDIR(mode):
        raise ValidationFailed("path", "must be a file, not a directory")
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        raise ValidationFailed("path", "must be a regular file, not a device, pipe or socket")
