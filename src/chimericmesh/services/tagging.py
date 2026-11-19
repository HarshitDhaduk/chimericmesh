"""Pure event-to-service tagging (R4). Lookup tables are rebuilt on every registry change."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import Service


@dataclass(frozen=True, slots=True)
class ServiceIndex:
    by_source: Mapping[str, str]                   # log_source_id -> service_id
    by_app: Mapping[str, tuple[str, ...]]          # casefolded app name -> service ids, creation order
    by_host: Mapping[str, tuple[str, ...]]         # casefolded host -> service ids, creation order


EMPTY_INDEX = ServiceIndex({}, {}, {})


def build_index(services: Sequence[Service]) -> ServiceIndex:
    by_source: dict[str, str] = {}
    by_app: dict[str, list[str]] = {}
    by_host: dict[str, list[str]] = {}
    for svc in sorted(services, key=lambda s: s.seq):
        for src in svc.log_sources:
            by_source[src.id] = svc.id
        for name in svc.app_names:
            ids = by_app.setdefault(name.casefold(), [])
            if svc.id not in ids:
                ids.append(svc.id)
        for host in svc.hosts:
            ids = by_host.setdefault(host.casefold(), [])
            if svc.id not in ids:
                ids.append(svc.id)
    return ServiceIndex(
        by_source,
        {k: tuple(v) for k, v in by_app.items()},
        {k: tuple(v) for k, v in by_host.items()},
    )


def resolve_service(index: ServiceIndex, source: str, host: str, app: str,
                    log_source_id: str | None) -> str | None:
    """R4.2: a known ``log_source_id``, then ``app``, then ``host``, else None. Total for any input.

    ``source`` is accepted for the signature in R4.1 and is not used (network listeners are global).
    """
    try:
        if isinstance(log_source_id, str):
            found = index.by_source.get(log_source_id)
            if found is not None:
                return found
        if isinstance(app, str):
            ids = index.by_app.get(app.casefold())
            if ids:
                return ids[0]
        if isinstance(host, str):
            ids = index.by_host.get(host.casefold())
            if ids:
                return ids[0]
    except Exception:  # defensive: tagging must never raise
        return None
    return None
