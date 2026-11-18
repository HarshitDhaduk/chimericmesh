"""Owner-only file permissions. The only permission code, and with ``discovery`` the only code
that starts a process (``icacls`` on Windows). Failures log one warning and never raise."""
from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("chimericmesh.fsperms")

_SAFE_USER = re.compile(r"^[\w.\- $@\\]+$")
_ICACLS_TIMEOUT = 10


def _secure(path: str | os.PathLike[str], *, directory: bool, os_name: str | None,
            runner: Callable[..., object] | None, user: str | None,
            chmod: Callable[[str, int], None] | None) -> bool:
    target = os.path.abspath(path)
    name = os.name if os_name is None else os_name
    if name == "posix":
        try:
            (chmod or os.chmod)(target, 0o700 if directory else 0o600)
            return True
        except OSError as exc:
            log.warning("could not set owner-only mode on %s: %s", target, type(exc).__name__)
            return False
    if name == "nt":
        who = getpass.getuser() if user is None else user
        if not who or not _SAFE_USER.fullmatch(who):
            log.warning("skipping owner-only ACL for %s: unusable user name", target)
            return False
        grant = f"{who}:(OI)(CI)F" if directory else f"{who}:F"
        cmd = ["icacls", target, "/inheritance:r", "/grant:r", grant]
        try:
            result = (runner or subprocess.run)(
                cmd, shell=False, timeout=_ICACLS_TIMEOUT, capture_output=True, text=True, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not set owner-only ACL on %s: %s", target, type(exc).__name__)
            return False
        if getattr(result, "returncode", 1) != 0:
            log.warning("icacls failed for %s (exit %s)", target, getattr(result, "returncode", "?"))
            return False
        return True
    log.warning("owner-only permissions are not supported on this platform (%s)", name)
    return False


def secure_dir(path: str | os.PathLike[str], *, os_name: str | None = None,
               runner: Callable[..., object] | None = None, user: str | None = None,
               chmod: Callable[[str, int], None] | None = None) -> bool:
    """Make ``path`` (a directory) accessible to the current user only. True when applied."""
    return _secure(path, directory=True, os_name=os_name, runner=runner, user=user, chmod=chmod)


def secure_file(path: str | os.PathLike[str], *, os_name: str | None = None,
                runner: Callable[..., object] | None = None, user: str | None = None,
                chmod: Callable[[str, int], None] | None = None) -> bool:
    """Make ``path`` (a file) accessible to the current user only. True when applied."""
    return _secure(path, directory=False, os_name=os_name, runner=runner, user=user, chmod=chmod)


def secure_sidecars(db_path: str | os.PathLike[str], **kw: object) -> None:
    """Secure the ``-wal`` and ``-shm`` files SQLite created next to the database, if present."""
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            secure_file(side, **kw)  # type: ignore[arg-type]
