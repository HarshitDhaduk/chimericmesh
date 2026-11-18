"""DDL of migration ``store:0001-core-tables``. The ``migrations`` table is not here: only the
``apply_migrations`` bootstrap creates it."""
from __future__ import annotations

from .migrations import MigrationRegistry

CORE_TABLES_DDL: tuple[str, ...] = (
    """CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  ts            REAL    NOT NULL,
  source        TEXT    NOT NULL,
  host          TEXT    NOT NULL,
  app           TEXT    NOT NULL,
  severity      INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 7),
  message       TEXT    NOT NULL,
  raw           TEXT    NOT NULL DEFAULT '',
  service_id    TEXT,
  log_source_id TEXT,
  ingested      REAL    NOT NULL
)""",
    "CREATE INDEX idx_events_ingested ON events (ingested)",
    "CREATE INDEX idx_events_service ON events (service_id, id)",
    """CREATE TABLE incidents (
  id              TEXT PRIMARY KEY,
  host            TEXT    NOT NULL CHECK (length(host) <= 255),
  service_id      TEXT,
  category        TEXT    NOT NULL,
  signature       TEXT    NOT NULL,
  severity        INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 7),
  count           INTEGER NOT NULL CHECK (count >= 0),
  first_seen      REAL    NOT NULL,
  last_seen       REAL    NOT NULL,
  sample          TEXT    NOT NULL CHECK (length(sample) <= 4096),
  sources         TEXT    NOT NULL DEFAULT '[]',
  resolved        INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
  resolved_at     REAL,
  opening         INTEGER NOT NULL DEFAULT 1 CHECK (opening >= 1),
  opened          INTEGER NOT NULL DEFAULT 0 CHECK (opened IN (0, 1)),
  announced_count INTEGER,
  CHECK (resolved = 0 OR resolved_at IS NOT NULL)
)""",
    "CREATE INDEX idx_incidents_service ON incidents (service_id, resolved)",
    "CREATE INDEX idx_incidents_last ON incidents (last_seen)",
    """CREATE TABLE proposals (
  id                  TEXT PRIMARY KEY,
  seq                 INTEGER NOT NULL UNIQUE,
  incident_id         TEXT    NOT NULL,
  action              TEXT    NOT NULL,
  details             TEXT    NOT NULL DEFAULT '',
  status              TEXT    NOT NULL CHECK (status IN ('pending','approved','denied','done')),
  created             REAL    NOT NULL,
  decided_by          TEXT    NOT NULL DEFAULT '',
  decided_at          REAL,
  result_note         TEXT    NOT NULL DEFAULT '',
  decision_origin     TEXT    CHECK (decision_origin IN ('user','hub')),
  service_id          TEXT,
  incident_opening    INTEGER,
  approved_by_rule    TEXT,
  kind                TEXT,
  files               TEXT,
  estimated_lines     INTEGER,
  verification        TEXT,
  files_changed       TEXT,
  lines_changed       INTEGER,
  verification_result TEXT
)""",
    "CREATE INDEX idx_proposals_status ON proposals (status)",
    "CREATE INDEX idx_proposals_incident ON proposals (incident_id, status)",
    "CREATE INDEX idx_proposals_service ON proposals (service_id, created)",
    """CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  subject     TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  service_id  TEXT,
  rule_id     TEXT,
  proposal_id TEXT
)""",
    "CREATE INDEX idx_audit_service ON audit_log (service_id, id)",
    "CREATE INDEX idx_audit_rule ON audit_log (rule_id, id)",
    "CREATE INDEX idx_audit_proposal ON audit_log (proposal_id, id)",
    """CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TABLE counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
)""",
)


def register_core_migrations(registry: MigrationRegistry) -> None:
    registry.register("store", 1, "store:0001-core-tables", CORE_TABLES_DDL)


def core_registry() -> MigrationRegistry:
    registry = MigrationRegistry()
    register_core_migrations(registry)
    return registry
