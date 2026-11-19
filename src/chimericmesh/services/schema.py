"""Migration ``services:0100-registry``: services, log sources and deleted-id tombstones."""
from __future__ import annotations

from chimericmesh.store.migrations import MigrationRegistry
from chimericmesh.store.schema import core_registry

SERVICES_DDL: tuple[str, ...] = (
    """CREATE TABLE services (
  id          TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 40 AND id <> 'unassigned'),
  seq         INTEGER NOT NULL UNIQUE,
  name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  hosts       TEXT NOT NULL DEFAULT '[]',
  app_names   TEXT NOT NULL DEFAULT '[]',
  protocols   TEXT NOT NULL DEFAULT '[]',
  code_folder TEXT CHECK (code_folder IS NULL OR length(code_folder) <= 1024),
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
)""",
    """CREATE TABLE log_sources (
  id         TEXT PRIMARY KEY,
  seq        INTEGER NOT NULL UNIQUE,
  service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  path       TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 1024),
  format     TEXT NOT NULL CHECK (length(format) BETWEEN 1 AND 30),
  enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  backfill_lines INTEGER NOT NULL DEFAULT 0 CHECK (backfill_lines BETWEEN 0 AND 1000),
  origin     TEXT NOT NULL CHECK (origin IN ('user', 'discovery')),
  host       TEXT CHECK (host IS NULL OR length(host) <= 100),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
)""",
    "CREATE INDEX idx_log_sources_service ON log_sources (service_id, seq)",
    """CREATE TABLE service_tombstones (
  id         TEXT PRIMARY KEY,
  deleted_at REAL NOT NULL
)""",
)


def register_migrations(registry: MigrationRegistry) -> None:
    registry.register("services", 100, "services:0100-registry", SERVICES_DDL)


def services_registry() -> MigrationRegistry:
    """Core plus services migrations."""
    registry = core_registry()
    register_migrations(registry)
    return registry
