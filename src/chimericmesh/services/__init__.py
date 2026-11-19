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

__all__ = [
    "AccessConfigError", "BuiltinService", "CLEAR", "Conflict", "LogSource", "LogSourceFields",
    "LogSourceNotFound", "LogSourcePatch", "Service", "ServiceChange", "ServiceFields",
    "ServiceNotFound", "ServicePatch", "ServicesError", "ServicesNotMigrated", "SourceRuntime",
    "StatusProvider", "SubscriberFailed", "ValidationFailed", "path_in_code_folder",
]
