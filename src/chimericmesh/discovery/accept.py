"""Accept a suggestion: validate everything first, then create everything in one atomic registry call (R5).

``code_folder`` is only ever what the user typed (O16): discovery never guesses, derives or pre-fills one. It is
passed unchanged to the registry, the single owner of its normalization and validation."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.models import LogSource, LogSourceFields, Service, ServiceFields
from chimericmesh.services.paths import inspect_log_path, normalize_log_path
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.validate import LIMITS, validate_log_source_fields, validate_service_fields
from chimericmesh.store import Store

from .errors import AlreadyAccepted, SuggestionNotFound
from .text import path_key

if TYPE_CHECKING:
    from .manager import ScanManager

log = logging.getLogger("chimericmesh.discovery")
MAX_PATHS = LIMITS.sources_per_service


@dataclass(frozen=True, slots=True)
class AcceptBody:
    id: str
    name: str
    log_paths: tuple[tuple[str, str], ...]                 # (path, format), 1 to 20
    code_folder: str | None = None
    app_names: tuple[str, ...] | None = None


class ServiceSink(Protocol):
    async def create_with_sources(self, fields: ServiceFields, sources: Sequence[LogSourceFields], *,
                                  actor: str) -> tuple[Service, list[LogSource]]: ...


class RegistrySink:
    """One call to the atomic ``create_service_with_sources``: all or nothing, no tombstone on failure."""

    def __init__(self, registry: ServiceRegistry) -> None:
        self._registry = registry

    async def create_with_sources(self, fields: ServiceFields, sources: Sequence[LogSourceFields], *,
                                  actor: str) -> tuple[Service, list[LogSource]]:
        return await self._registry.create_service_with_sources(fields, sources, actor=actor, origin="discovery")


class Acceptor:
    def __init__(self, manager: "ScanManager", sink: ServiceSink, registry: ServiceRegistry, store: Store,
                 known_formats: frozenset[str]) -> None:
        self._manager, self._sink, self._registry = manager, sink, registry
        self._store, self._known_formats = store, known_formats

    def _validate(self, body: AcceptBody, process: str | None) -> tuple[ServiceFields, list[LogSourceFields]]:
        """Validate everything and create nothing (R5.5)."""
        if not isinstance(body.log_paths, (tuple, list)) or not 1 <= len(body.log_paths) <= MAX_PATHS:
            raise ValidationFailed("log_paths", f"select 1 to {MAX_PATHS} log paths")
        raw: dict[str, object] = {"id": body.id, "name": body.name, "protocols": ["file"]}
        if body.app_names is not None:
            raw["app_names"] = list(body.app_names)
        elif process:
            raw["app_names"] = [process]                    # O8: tag Syslog events of that app too
        if body.code_folder is not None:
            raw["code_folder"] = body.code_folder           # user-typed; the registry validates it
        fields = validate_service_fields(raw, for_create=True)
        assert isinstance(fields, ServiceFields)
        os_name = getattr(self._manager, "os_name", None) or "linux"
        seen: set[str] = set()
        sources: list[LogSourceFields] = []
        for path, fmt in body.log_paths:
            parsed = validate_log_source_fields({"path": path, "format": fmt, "backfill_lines": 0},
                                                known_formats=self._known_formats, for_create=True)
            assert isinstance(parsed, LogSourceFields)
            normalized = normalize_log_path(parsed.path)
            inspect_log_path(normalized)                     # stat only: a directory or a device is refused
            key = path_key(normalized, os_name)
            if key in seen:
                raise ValidationFailed("log_paths", "the same path is listed twice")
            seen.add(key)
            sources.append(LogSourceFields(path=normalized, format=parsed.format, enabled=True,
                                           host=None, backfill_lines=0))
        if len(self._registry.log_sources()) + len(sources) > LIMITS.sources_total:
            raise ValidationFailed("log_paths", "too many log sources")
        if len(self._registry.list()) >= LIMITS.services:
            raise ValidationFailed("id", "too many services")
        return fields, sources

    async def accept(self, suggestion_id: str, body: AcceptBody, *, actor: str) -> tuple[Service, list[LogSource]]:
        found = self._manager.find(suggestion_id)
        if found is None:
            raise SuggestionNotFound(suggestion_id)
        scan, sug = found
        # One await-free block: two concurrent accepts of one suggestion cannot both pass.
        if suggestion_id in scan.accepted or suggestion_id in scan.accepting:
            raise AlreadyAccepted(suggestion_id)
        scan.accepting.add(suggestion_id)
        try:
            fields, sources = self._validate(body, sug.evidence.process)
            service, created = await self._sink.create_with_sources(fields, sources, actor="discovery")
            scan.accepted[suggestion_id] = service.id
        finally:
            scan.accepting.discard(suggestion_id)
        try:
            await self._store.audit.append(actor, "discovery_accepted", sug.app_id, f"paths={len(created)}",
                                           service_id=service.id)
        except Exception:  # noqa: BLE001 - the service exists; a failing audit write must not hide that
            log.error("discovery: the audit row for accepted suggestion %s could not be written", suggestion_id)
        return service, created
