"""Store error types. All of them derive from StoreError so callers can catch one class."""
from __future__ import annotations


class StoreError(Exception):
    """Base class for every error raised by the store."""


class StoreOpenError(StoreError):
    """The database could not be opened (environment, SQLite version, WAL unavailable)."""


class StoreCorruptError(StoreOpenError):
    """The file exists but is not a valid SQLite database. The file is left untouched."""

    def __init__(self, path: str, detail: str = "") -> None:
        self.path = path
        super().__init__(f"{path} is not a valid SQLite database" + (f": {detail}" if detail else ""))


class SchemaTooNewError(StoreOpenError):
    """The database was written by a newer build (user_version above the highest known)."""

    def __init__(self, path: str, found: int, known: int) -> None:
        self.path = path
        self.found = found
        self.known = known
        super().__init__(f"{path} has schema version {found}, this build knows up to {known}")


class MigrationError(StoreError):
    """A migration is invalid or failed; the failing migration was rolled back."""

    def __init__(self, name: str, cause: BaseException | str | None = None) -> None:
        self.name = name
        self.cause = cause
        super().__init__(f"migration {name}: {cause}" if cause else f"migration {name}")


class StoreWriteError(StoreError):
    """A write failed after the retries were used up."""


class StoreBusyError(StoreWriteError):
    """The database stayed busy or locked."""


class StoreFullError(StoreWriteError):
    """The disk is full."""


class StoreClosedError(StoreError):
    """The store was used after close."""


class StoreReentryError(StoreError):
    """A job was submitted from the writer thread (it would wait for itself)."""


class InvalidTransition(StoreError):
    """A proposal status change that the transition table does not allow."""


class NotFound(StoreError):
    """The requested row does not exist."""
