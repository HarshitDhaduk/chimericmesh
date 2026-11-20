"""Errors raised by the discovery module."""
from __future__ import annotations


class DiscoveryError(Exception):
    """Base class for every error raised by ``chimericmesh.discovery``."""


class CatalogError(DiscoveryError):
    """The catalog is invalid."""


class CatalogUnavailable(DiscoveryError):
    """The catalog could not be loaded: discovery is disabled (REST 503)."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(reason or "catalog unavailable")


class ScanBusy(DiscoveryError):
    """A scan is already running (REST 409)."""

    def __init__(self, scan_id: str) -> None:
        self.scan_id = scan_id
        super().__init__(f"scan {scan_id} is running")


class SuggestionNotFound(DiscoveryError):
    """Unknown or expired suggestion id (REST 404)."""


class AlreadyAccepted(DiscoveryError):
    """The suggestion is accepted or being accepted (REST 409)."""
