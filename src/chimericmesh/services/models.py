"""Immutable snapshots shared by the registry, its subscribers and the status view."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


class _Clear:
    """Sentinel: remove an optional field (JSON ``null`` in a PATCH body)."""

    _instance: "_Clear | None" = None

    def __new__(cls) -> "_Clear":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "CLEAR"

    def __bool__(self) -> bool:
        return False


CLEAR = _Clear()

Origin = Literal["user", "discovery"]


@dataclass(frozen=True, slots=True)
class LogSource:
    id: str                        # "ls<n>", durable, never reused
    service_id: str
    path: str                      # absolute, normalized (os.path.normpath), not symlink-resolved
    format: str
    enabled: bool
    backfill_lines: int            # 0 to 1000; used by connectors on the first attach only
    origin: Origin
    host: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id, "service_id": self.service_id, "path": self.path, "format": self.format,
            "enabled": self.enabled, "backfill_lines": self.backfill_lines, "origin": self.origin,
            "host": self.host, "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class Service:
    id: str
    name: str
    hosts: tuple[str, ...]
    app_names: tuple[str, ...]
    protocols: tuple[str, ...]
    log_sources: tuple[LogSource, ...]       # ordered by seq
    code_folder: str | None                  # resolved, absolute
    created_at: float
    updated_at: float
    seq: int                                 # creation order; not in to_dict()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "hosts": list(self.hosts),
            "app_names": list(self.app_names), "protocols": list(self.protocols),
            "log_sources": [s.to_dict() for s in self.log_sources],
            "code_folder": self.code_folder, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ServiceFields:
    """Create input, already validated."""

    id: str
    name: str
    hosts: tuple[str, ...] = ()
    app_names: tuple[str, ...] = ()
    protocols: tuple[str, ...] = ()
    code_folder: str | None = None


@dataclass(frozen=True, slots=True)
class ServicePatch:
    """None means unchanged; ``code_folder`` uses CLEAR to remove the folder."""

    name: str | None = None
    hosts: tuple[str, ...] | None = None
    app_names: tuple[str, ...] | None = None
    protocols: tuple[str, ...] | None = None
    code_folder: str | None | _Clear = None


@dataclass(frozen=True, slots=True)
class LogSourceFields:
    path: str
    format: str
    enabled: bool = True
    host: str | None = None
    backfill_lines: int = 0


@dataclass(frozen=True, slots=True)
class LogSourcePatch:
    format: str | None = None
    enabled: bool | None = None
    host: str | None | _Clear = None
    backfill_lines: int | None = None


ChangeKind = Literal[
    "service_created", "service_updated", "service_deleted",
    "log_source_added", "log_source_updated", "log_source_removed",
]


@dataclass(frozen=True, slots=True)
class ServiceChange:
    kind: ChangeKind
    service_id: str
    old: Service | None                      # None for service_created
    new: Service | None                      # None for service_deleted
    log_source_id: str | None = None
    fields: tuple[str, ...] = ()             # names of changed fields, never values

    @property
    def old_code_folder(self) -> str | None:
        return self.old.code_folder if self.old is not None else None

    @property
    def new_code_folder(self) -> str | None:
        return self.new.code_folder if self.new is not None else None

    @property
    def code_folder_changed(self) -> bool:
        if self.old is None or self.new is None:
            return False
        return self.old.code_folder != self.new.code_folder


@dataclass(frozen=True, slots=True)
class SourceRuntime:
    """What connectors supplies per log source."""

    status: Literal["running", "missing", "unreadable", "stopped", "error", "unknown"] = "unknown"
    lines_read: int = 0
    events_emitted: int = 0
    lines_dropped: int = 0
    bytes_read: int = 0
    unparsed: int = 0
    last_error: str = ""                     # cut to 200 characters on read


StatusProvider = Callable[[str], "SourceRuntime | None"]
