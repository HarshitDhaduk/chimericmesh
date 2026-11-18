"""Store configuration: five documented environment variables plus timing knobs for tests."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger("chimericmesh.store")

DB_FILENAME = "chimericmesh.db"
MEMORY = ":memory:"

DEFAULT_MAX_EVENTS = 100_000
DEFAULT_EVENT_TTL_DAYS = 7
DEFAULT_REOPEN_AFTER = 300

_DIGITS = re.compile(r"[0-9]{1,18}")


def _env_int(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    if _DIGITS.fullmatch(raw) and low <= int(raw) <= high:
        return int(raw)
    log.warning("%s=%r is not an integer in %d..%d; using %d", name, raw[:40], low, high, default)
    return default


@dataclass(frozen=True)
class StoreConfig:
    max_events: int = DEFAULT_MAX_EVENTS
    event_ttl_days: int = DEFAULT_EVENT_TTL_DAYS
    reopen_after: int = DEFAULT_REOPEN_AFTER
    batch_rows: int = 500
    batch_ms: int = 250
    queue_limit: int = 20000
    trim_interval: float = 60.0
    flush_interval: float = 30.0
    flush_tick: float = 5.0
    persist_slots: int = 256

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: object) -> "StoreConfig":
        env = os.environ if environ is None else environ
        cfg = cls(
            max_events=_env_int(env, "CHIMERICMESH_MAX_EVENTS", DEFAULT_MAX_EVENTS, 1, 10_000_000),
            event_ttl_days=_env_int(env, "CHIMERICMESH_EVENT_TTL_DAYS", DEFAULT_EVENT_TTL_DAYS, 1, 3650),
            reopen_after=_env_int(env, "CHIMERICMESH_REOPEN_AFTER_SECONDS", DEFAULT_REOPEN_AFTER, 0, 86400),
        )
        return replace(cfg, **overrides) if overrides else cfg  # type: ignore[arg-type]


PROPOSAL_ID_NOTE = "Proposal ids are unique per database; deleting the database restarts them at p1."


def describe_startup(path: str) -> str:
    """The one startup log line: where the data lives and what that means for proposal ids."""
    if path == MEMORY:
        return f"store: in-memory database, nothing is saved. {PROPOSAL_ID_NOTE}"
    return f"store: database at {path}. {PROPOSAL_ID_NOTE}"


def env_reopen_after(environ: Mapping[str, str] | None = None) -> int:
    """``CHIMERICMESH_REOPEN_AFTER_SECONDS`` (0 to 86400); a bad value warns and gives 300."""
    env = os.environ if environ is None else environ
    return _env_int(env, "CHIMERICMESH_REOPEN_AFTER_SECONDS", DEFAULT_REOPEN_AFTER, 0, 86400)


def resolve_home(environ: Mapping[str, str] | None = None) -> str:
    """Absolute per-user home directory. Never derived from the working directory by default."""
    env = os.environ if environ is None else environ
    raw = env.get("CHIMERICMESH_HOME") or "~/.chimericmesh"
    return os.path.abspath(os.path.expanduser(raw))


def resolve_db_path(environ: Mapping[str, str] | None = None) -> str:
    """Absolute database path, or ``:memory:``. Pure: touches no file."""
    env = os.environ if environ is None else environ
    raw = env.get("CHIMERICMESH_DB")
    if raw == MEMORY:
        return MEMORY
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return str(Path(resolve_home(env)) / DB_FILENAME)
