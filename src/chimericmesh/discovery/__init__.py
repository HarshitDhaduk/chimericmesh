"""Opt-in, read-only scan of the local machine that suggests services. Pure re-exports only: this
package imports no web framework (``routes.py`` is imported by the wiring)."""
from __future__ import annotations

from .errors import (
    AlreadyAccepted,
    CatalogError,
    CatalogUnavailable,
    DiscoveryError,
    ScanBusy,
    SuggestionNotFound,
)

__all__ = [
    "AlreadyAccepted", "CatalogError", "CatalogUnavailable", "DiscoveryError", "ScanBusy",
    "SuggestionNotFound",
]
