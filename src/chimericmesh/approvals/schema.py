"""Migration ``approvals:0400-rules``: rules, their uses, first-seen openings and the pause flag."""
from __future__ import annotations

from chimericmesh.services.schema import services_registry
from chimericmesh.store.migrations import MigrationRegistry

APPROVALS_DDL: tuple[str, ...] = (
    """CREATE TABLE approval_rules (
  id                   TEXT PRIMARY KEY,
  seq                  INTEGER NOT NULL UNIQUE,
  service_id           TEXT    NOT NULL,
  match_type           TEXT    NOT NULL CHECK (match_type IN ('signature','category')),
  match_value          TEXT    NOT NULL CHECK (length(match_value) BETWEEN 1 AND 200),
  most_urgent_severity INTEGER NOT NULL CHECK (most_urgent_severity IN (3,4)),
  expires_at           REAL    NOT NULL,
  max_per_hour         INTEGER NOT NULL CHECK (max_per_hour BETWEEN 1 AND 10),
  max_per_incident     INTEGER NOT NULL CHECK (max_per_incident BETWEEN 1 AND 3),
  max_files            INTEGER NOT NULL CHECK (max_files BETWEEN 1 AND 20),
  max_lines            INTEGER NOT NULL CHECK (max_lines BETWEEN 1 AND 500),
  status               TEXT    NOT NULL CHECK (status IN ('active','suspended','revoked','expired')),
  status_reason        TEXT    NOT NULL DEFAULT '',
  created_by           TEXT    NOT NULL,
  created_at           REAL    NOT NULL,
  updated_at           REAL    NOT NULL,
  source_proposal_id   TEXT,
  note                 TEXT    NOT NULL DEFAULT '' CHECK (length(note) <= 200),
  use_count            INTEGER NOT NULL DEFAULT 0,
  code_folder          TEXT    NOT NULL
)""",
    "CREATE INDEX idx_rules_service ON approval_rules (service_id, status)",
    """CREATE TABLE rule_uses (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id          TEXT    NOT NULL,
  proposal_id      TEXT    NOT NULL UNIQUE,
  incident_id      TEXT    NOT NULL,
  opening          INTEGER NOT NULL,
  service_id       TEXT,
  approved_at      REAL    NOT NULL,
  completed_at     REAL
)""",
    "CREATE INDEX idx_uses_rule ON rule_uses (rule_id, approved_at)",
    "CREATE INDEX idx_uses_incident ON rule_uses (incident_id, opening)",
    """CREATE TABLE incident_openings (
  incident_id    TEXT    NOT NULL,
  opening        INTEGER NOT NULL,
  first_seen_at  REAL    NOT NULL,
  PRIMARY KEY (incident_id, opening)
)""",
    """CREATE TABLE approval_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
)""",
)


def register_migrations(registry: MigrationRegistry) -> None:
    registry.register("approvals", 400, "approvals:0400-rules", APPROVALS_DDL)


def approvals_registry() -> MigrationRegistry:
    """Core, services and approvals migrations (what the tests of this module open)."""
    registry = services_registry()
    register_migrations(registry)
    return registry
