"""Service registry, tagging and access control. Re-exports pure names only: importing this package
loads no web framework (``deps.py`` and ``routes.py`` are imported by the wiring)."""
from __future__ import annotations

from .errors import (
    AccessConfigError,
    BuiltinService,
    Conflict,
    LogSourceNotFound,
    ServiceNotFound,
    ServicesError,
    ServicesNotMigrated,
    SubscriberFailed,
    ValidationFailed,
)
from .models import (
    CLEAR,
    LogSource,
    LogSourceFields,
    LogSourcePatch,
    Service,
    ServiceChange,
    ServiceFields,
    ServicePatch,
    SourceRuntime,
    StatusProvider,
)
from .paths import path_in_code_folder
from .registry import ServiceRegistry
from .schema import register_migrations, services_registry
from .tagging import ServiceIndex, build_index, resolve_service

__all__ = [
    "AccessConfigError", "BuiltinService", "CLEAR", "Conflict", "LogSource", "LogSourceFields",
    "LogSourceNotFound", "LogSourcePatch", "Service", "ServiceChange", "ServiceFields",
    "ServiceNotFound", "ServicePatch", "ServicesError", "ServicesNotMigrated", "SourceRuntime",
    "StatusProvider", "SubscriberFailed", "ValidationFailed", "path_in_code_folder", "ServiceRegistry",
    "register_migrations", "services_registry", "ServiceIndex", "build_index", "resolve_service",
]
