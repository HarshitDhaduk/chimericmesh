"""Pure, total text helpers. Process names and file names are untrusted (R4.7)."""
from __future__ import annotations

import ntpath
import posixpath
import re
from typing import Literal

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
MAX_NAME = 64
OsName = Literal["windows", "linux", "macos"]


def clean_text(value: object, limit: int = 1024) -> str:
    """str() on non-text, remove control characters and lone surrogates, strip, cut. Never raises."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = str(value)
        except Exception:  # noqa: BLE001 - a hostile __str__ must not break a scan
            return ""
    return CONTROL.sub("", text).strip()[:limit]


def normalize_process_name(raw: object, os_name: str) -> str:
    """R4.1: no directory, casefolded, no trailing ``.exe``, at most 64 characters. Idempotent."""
    text = clean_text(raw, 4096)
    text = re.split(r"[\\/]", text)[-1]
    text = text.casefold()[:MAX_NAME]
    while True:
        text = text.strip()
        if text.endswith(".exe"):
            text = text[:-4]
        else:
            return text


def path_key(path: str, os_name: str) -> str:
    """Comparison key for a path: case and separator insensitive on Windows only."""
    if os_name == "windows":
        return ntpath.normcase(ntpath.normpath(path))
    return posixpath.normpath(path)


def os_name_from_platform(platform: str) -> OsName | None:
    if platform in ("win32", "cygwin"):
        return "windows"
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "macos"
    return None
