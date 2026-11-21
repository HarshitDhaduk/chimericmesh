"""Migration ``discovery:0300-dismissals``: the only table this module adds."""
from __future__ import annotations

from chimericmesh.services.schema import services_registry
from chimericmesh.store.migrations import MigrationRegistry

DISMISSALS_DDL: tuple[str, ...] = (
    """CREATE TABLE discovery_dismissals (
  app_id       TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 40),
  fingerprint  TEXT NOT NULL CHECK (length(fingerprint) = 16),
  dismissed_at REAL NOT NULL,
  PRIMARY KEY (app_id, fingerprint)
)""",
)


def register_migrations(registry: MigrationRegistry) -> None:
    registry.register("discovery", 300, "discovery:0300-dismissals", DISMISSALS_DDL)


def discovery_registry() -> MigrationRegistry:
    """Core, services and discovery migrations."""
    registry = services_registry()
    register_migrations(registry)
    return registry
