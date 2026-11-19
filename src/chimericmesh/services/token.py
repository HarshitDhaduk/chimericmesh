"""The admin token: one random value in an owner-only file (R6.1, R6.2, R6.8).

The token is read once at open. ``rotate`` replaces the file; a running hub keeps its old token until
it restarts. The value is never part of ``repr`` and is handed to the audit redaction list at open.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from chimericmesh import fsperms

from .errors import AccessConfigError

log = logging.getLogger("chimericmesh.services")

TOKEN_FILENAME = "admin.token"
TOKEN_FILE_ENV = "CHIMERICMESH_TOKEN_FILE"
_VALID = re.compile(r"[\x21-\x7e]{32,200}")
_RACE_WAIT = 1.0


def resolve_token_path(environ: Mapping[str, str], home: Path) -> Path:
    raw = environ.get(TOKEN_FILE_ENV)
    return Path(os.path.abspath(os.path.expanduser(raw))) if raw else Path(home) / TOKEN_FILENAME


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _atomic_write(path: Path, token: str) -> None:
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token + "\n")
        fsperms.secure_file(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _under(path: Path, cwd: Path) -> bool:
    try:
        path.resolve().relative_to(cwd.resolve())
        return True
    except (ValueError, OSError):
        return False


class TokenStore:
    def __init__(self, path: Path | None, token: str) -> None:
        self.path = path
        self._token = token

    @classmethod
    def open(cls, path: Path | None, *, in_memory: bool, cwd: Path | None = None,
             redact: Callable[[str], None] | None = None) -> "TokenStore":
        if in_memory or path is None:
            store = cls(None, _new_token())
        else:
            path = Path(path)
            if _under(path, cwd if cwd is not None else Path.cwd()):
                log.warning("the admin token file %s is under the working directory; "
                            "use a per-user location", path)
            store = cls(path, _open_file(path))
        if redact is not None:
            redact(store._token)
        return store

    def current(self) -> str:
        return self._token

    def rotate(self) -> None:
        new = _new_token()
        if self.path is None:
            self._token = new
            return
        _atomic_write(self.path, new)

    def __repr__(self) -> str:
        return f"TokenStore(path={str(self.path)!r})"


def _open_file(path: Path) -> str:
    parent = path.parent
    if not parent.is_dir():
        os.makedirs(parent, exist_ok=True)
        fsperms.secure_dir(parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_existing(path)
    token = _new_token()
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(token + "\n")
    fsperms.secure_file(path)
    return token


def _read_existing(path: Path) -> str:
    deadline = time.monotonic() + _RACE_WAIT
    text = _read(path)
    while not text and time.monotonic() < deadline:     # the creating process may not have written yet
        time.sleep(0.02)
        text = _read(path)
    if not text:
        token = _new_token()
        _atomic_write(path, token)
        return token
    if _VALID.fullmatch(text) is None:
        raise AccessConfigError(
            f"admin token file {path} is not a valid token; "
            "run `python -m chimericmesh token --rotate` to replace it")
    return text
