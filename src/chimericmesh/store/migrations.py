"""Migration registry. Modules register their migrations; the runner applies them at open."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .errors import MigrationError

MODULE_BLOCKS: dict[str, tuple[int, int]] = {
    "store": (1, 99),
    "services": (100, 199),
    "connectors": (200, 299),
    "discovery": (300, 399),
    "approvals": (400, 499),
    "agent": (500, 599),
    "dashboard": (600, 699),
}

Apply = tuple[str, ...] | Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    module: str
    version: int
    name: str
    apply: Apply


class MigrationRegistry:
    def __init__(self) -> None:
        self._by_version: dict[int, Migration] = {}
        self._names: set[str] = set()

    def register(self, module: str, version: int, name: str, apply: Apply) -> None:
        block = MODULE_BLOCKS.get(module)
        if block is None:
            raise MigrationError(name, f"unknown module {module!r}")
        low, high = block
        if not low <= version <= high:
            raise MigrationError(name, f"version {version} is outside the {module} block {low}-{high}")
        if version in self._by_version:
            raise MigrationError(name, f"duplicate version {version}")
        if name in self._names:
            raise MigrationError(name, "duplicate name")
        self._by_version[version] = Migration(module, version, name, apply)
        self._names.add(name)

    def migrations(self) -> list[Migration]:
        return [self._by_version[v] for v in sorted(self._by_version)]

    def highest(self) -> int:
        return max(self._by_version, default=0)
