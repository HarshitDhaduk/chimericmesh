"""Hash the working-tree bytes of a few files, so a later task can show it changed none of them (R1-L10)."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping


def _hash(path: str) -> str:
    try:
        if not os.path.isfile(path):
            return "absent"
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "absent"


def snapshot(paths: Iterable[str], root: str | None = None) -> dict[str, str]:
    """Path (as given) to the SHA-256 of the file bytes; a missing file or a directory is ``absent``."""
    return {p: _hash(os.path.join(root, p) if root else p) for p in paths}


def diff_snapshots(a: Mapping[str, str], b: Mapping[str, str]) -> list[str]:
    """Sorted paths whose value differs, or that exist in only one snapshot."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
