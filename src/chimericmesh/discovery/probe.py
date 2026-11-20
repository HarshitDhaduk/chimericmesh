"""The only place that touches candidate paths and ``/proc``: ``os.stat``, ``os.access``, ``os.scandir``
(entry names only) and one ``open`` of ``/proc/<int>/comm``. No method opens or reads a log file (R2.2)."""
from __future__ import annotations

import os
import stat
from typing import Protocol


class FsProbe(Protocol):
    def stat(self, path: str) -> tuple[bool, int | None, float | None]: ...
    def readable(self, path: str) -> bool: ...
    def listdir(self, directory: str, limit: int) -> tuple[list[str], bool]: ...


class ProcFs(Protocol):
    def pids(self) -> list[int]: ...
    def comm(self, pid: int) -> str | None: ...


class OsFsProbe:
    def stat(self, path: str) -> tuple[bool, int | None, float | None]:
        """``(exists, size_bytes, mtime)``. A directory exists but has no size."""
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return False, None, None
        size = int(st.st_size) if stat.S_ISREG(st.st_mode) else None
        return True, size, float(st.st_mtime)

    def readable(self, path: str) -> bool:
        try:
            return bool(os.access(path, os.R_OK))
        except (OSError, ValueError):
            return False

    def listdir(self, directory: str, limit: int) -> tuple[list[str], bool]:
        """At most ``limit`` entry names (never their contents or metadata); ``True`` when more existed."""
        names: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(names) >= limit:
                        return names, True
                    names.append(entry.name)
        except (OSError, ValueError):
            return names, False
        return names, False


class LinuxProcFs:
    def __init__(self, root: str = "/proc") -> None:
        self._root = root

    def pids(self) -> list[int]:
        try:
            with os.scandir(self._root) as entries:
                found = [int(e.name) for e in entries if e.name.isdigit() and len(e.name) <= 10]
        except (OSError, ValueError):
            return []
        return sorted(found)

    def comm(self, pid: int) -> str | None:
        path = f"{self._root}/{int(pid)}/comm"          # built from an int: no caller text reaches open()
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read(256).strip()
        except OSError:
            return None
