"""Migration ``connectors:0200-tail-offsets``: the persisted read position of each log source."""
from __future__ import annotations

from ..services.schema import services_registry
from ..store.migrations import MigrationRegistry

# No foreign key into log_sources on purpose (design "SQLite schema"); prune removes orphans. dev and ino are
# decimal text because Windows file ids can use all 64 bits.
TAIL_OFFSETS_DDL: tuple[str, ...] = (
    """CREATE TABLE tail_offsets (
  log_source_id TEXT PRIMARY KEY CHECK (length(log_source_id) BETWEEN 1 AND 30),
  dev           TEXT NOT NULL,
  ino           TEXT NOT NULL,
  head_len      INTEGER NOT NULL CHECK (head_len BETWEEN 0 AND 256),
  head_sha      TEXT NOT NULL CHECK (length(head_sha) = 64),
  byte_offset   INTEGER NOT NULL CHECK (byte_offset >= 0),
  discarding    INTEGER NOT NULL DEFAULT 0 CHECK (discarding IN (0, 1)),
  updated_at    REAL NOT NULL
)""",
)


def register_migrations(registry: MigrationRegistry) -> None:
    registry.register("connectors", 200, "connectors:0200-tail-offsets", TAIL_OFFSETS_DDL)


def connectors_registry() -> MigrationRegistry:
    """Core, services and connectors migrations."""
    registry = services_registry()
    register_migrations(registry)
    return registry
