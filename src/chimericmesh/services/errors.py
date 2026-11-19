"""Errors raised by the services module. None of them derives from a store error."""
from __future__ import annotations


class ServicesError(Exception):
    """Base class for every error raised by ``chimericmesh.services``."""


class ValidationFailed(ServicesError):
    """An input is invalid. ``field`` names the offending field (REST 422 with ``field``)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class Conflict(ServicesError):
    """The id exists, or belonged to a deleted service (REST 409)."""


class ServiceNotFound(ServicesError):
    """Unknown service id (REST 404)."""


class LogSourceNotFound(ServicesError):
    """Unknown log source id (REST 404)."""


class BuiltinService(ServicesError):
    """The built-in ``unassigned`` pseudo service cannot be edited or deleted (REST 403)."""


class SubscriberFailed(ServicesError):
    """An in-transaction subscriber raised; the whole change was rolled back (REST 500)."""

    def __init__(self, name: str, cause: BaseException | None = None) -> None:
        self.name = name
        self.cause = cause
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(f"subscriber {name!r} failed{detail}")


class ServicesNotMigrated(ServicesError):
    """The store was opened without the services migrations."""


class AccessConfigError(ServicesError):
    """The admin token file cannot be created or read."""
