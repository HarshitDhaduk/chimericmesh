"""Open the SQLite connection: byte-level header guard, owner-only creation, pragmas, second guard."""
from __future__ import annotations

import logging
import os
import sqlite3
import struct

from chimericmesh import fsperms

from .config import MEMORY
from .errors import SchemaTooNewError, StoreCorruptError, StoreOpenError
from .migrations import MigrationRegistry

log = logging.getLogger("chimericmesh.store")

_MAGIC = b"SQLite format 3\x00"
_HEADER_SIZE = 100
_MIN_SQLITE = (3, 24)


def _header_user_version(path: str) -> int | None:
    """Read the header bytes only (no SQLite connection, nothing is written).

    Returns None for a missing or empty file (a new database), else ``user_version`` as stored in
    the main file. Raises StoreCorruptError when the file is not a SQLite database.
    """
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return None
    if size == 0:
        return None
    with open(path, "rb") as fh:
        head = fh.read(_HEADER_SIZE)
    if len(head) < _HEADER_SIZE or head[:16] != _MAGIC:
        raise StoreCorruptError(path)
    return struct.unpack(">i", head[60:64])[0]


def _create_owner_only(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        fsperms.secure_dir(parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
    fsperms.secure_file(path)


def open_connection(path: str, registry: MigrationRegistry) -> sqlite3.Connection:
    """Open ``path`` (absolute path or ``:memory:``) and return a ready connection.

    Writes nothing before the guards pass: the byte guard first, then the pragmas and a
    ``PRAGMA user_version`` check (a crash can leave the new value only in the ``-wal`` file).
    """
    if sqlite3.sqlite_version_info < _MIN_SQLITE:
        raise StoreOpenError(
            f"SQLite {'.'.join(map(str, sqlite3.sqlite_version_info))} is too old; "
            "3.24 or newer is required"
        )
    memory = path == MEMORY
    known = registry.highest()
    if not memory:
        found = _header_user_version(path)
        if found is not None and found > known:
            raise SchemaTooNewError(path, found, known)
        try:
            _create_owner_only(path)
        except OSError as exc:
            raise StoreOpenError(f"cannot create {path}: {type(exc).__name__}") from exc
    try:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as exc:
        raise StoreOpenError(f"cannot open {path}: {type(exc).__name__}") from exc
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if not memory and str(mode).lower() != "wal":
            raise StoreOpenError(f"{path}: SQLite did not enable WAL mode (got {mode!r})")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > known:
            raise SchemaTooNewError(path, current, known)
        if not memory:
            fsperms.secure_sidecars(path)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise StoreCorruptError(path, type(exc).__name__) from exc
    except BaseException:
        conn.close()
        raise
    return conn
