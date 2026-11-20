"""The only file that imports ``subprocess``. Runs the fixed OS tools (R2.1): absolute system paths
(never the current directory or the caller's PATH), ``shell=False``, closed stdin, a minimal environment, a
timeout, an output cap and a kill on timeout, overflow or cancel."""
from __future__ import annotations

import ntpath
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from .models import RunResult, ToolSpec

POSIX_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
DEFAULT_SYSTEM_ROOT = "C:\\Windows"
# The exact allow-list of tool names per OS: nothing else can be resolved.
TOOLS: dict[str, frozenset[str]] = {
    "windows": frozenset({"tasklist", "netstat"}),
    "linux": frozenset({"ss", "netstat"}),
    "macos": frozenset({"ps", "lsof"}),
}
_CHUNK = 65536


class CommandRunner(Protocol):
    def run(self, tool: ToolSpec, *, timeout: float, max_bytes: int, cancel: threading.Event) -> RunResult: ...


def _system_root(environ: Mapping[str, str], isdir: Callable[[str], bool]) -> str:
    raw = environ.get("SystemRoot") or environ.get("SYSTEMROOT")
    if isinstance(raw, str) and raw and "\x00" not in raw and ntpath.isabs(raw) and isdir(raw):
        return raw.rstrip("\\/") or DEFAULT_SYSTEM_ROOT
    return DEFAULT_SYSTEM_ROOT


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_tool(name: str, os_name: str, environ: Mapping[str, str], *,
                 exists: Callable[[str], bool] = _is_executable,
                 isdir: Callable[[str], bool] = os.path.isdir) -> str | None:
    """Absolute path of a system tool, or ``None``. Never consults the current directory or ``PATH``."""
    if not isinstance(name, str) or name not in TOOLS.get(os_name, frozenset()):
        return None
    if os_name == "windows":
        path = ntpath.join(_system_root(environ, isdir), "System32", name + ".exe")
        return path if exists(path) else None
    for directory in POSIX_DIRS:
        path = f"{directory}/{name}"
        if exists(path):
            return path
    return None


class SubprocessRunner:
    def __init__(self, os_name: str, environ: Mapping[str, str]) -> None:
        self._os = os_name
        self._environ = dict(environ)

    def _child_env(self) -> dict[str, str]:
        if self._os == "windows":
            root = _system_root(self._environ, os.path.isdir)
            return {"SystemRoot": root, "LC_ALL": "C", "PATH": ntpath.join(root, "System32")}
        return {"LC_ALL": "C", "PATH": ":".join(POSIX_DIRS)}

    def run(self, tool: ToolSpec, *, timeout: float, max_bytes: int, cancel: threading.Event) -> RunResult:
        exe = resolve_tool(tool.name, self._os, self._environ)
        if exe is None:
            return RunResult(False, "", "missing")
        return self.run_argv([exe, *tool.args], timeout=timeout, max_bytes=max_bytes, cancel=cancel,
                             env=self._child_env(), cwd=os.path.dirname(exe))

    def run_argv(self, argv: Sequence[str], *, timeout: float, max_bytes: int, cancel: threading.Event,
                 env: Mapping[str, str], cwd: str | None = None) -> RunResult:
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, cwd=cwd, env=dict(env), close_fds=True, **kwargs)
        except FileNotFoundError:
            return RunResult(False, "", "missing")
        except PermissionError:
            return RunResult(False, "", "denied")
        except (OSError, ValueError):
            return RunResult(False, "", "error")
        buf = bytearray()
        overflow = threading.Event()

        def reader() -> None:
            try:
                stream = proc.stdout
                while stream is not None:
                    chunk = stream.read1(_CHUNK)
                    if not chunk:
                        return
                    room = max_bytes - len(buf)
                    if room > 0:
                        buf.extend(chunk[:room])
                    if len(chunk) > room:
                        overflow.set()
                        return
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=reader, name="discovery-tool-reader", daemon=True)
        thread.start()
        reason = ""
        deadline = time.monotonic() + max(0.0, timeout)
        while proc.poll() is None:
            if cancel.is_set():
                reason = "cancelled"
                break
            if overflow.is_set():
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            cancel.wait(0.05)
        killed = proc.poll() is None
        if killed:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        thread.join(timeout=2.0)
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
        thread.join(timeout=0.5)
        if reason:
            return RunResult(False, "", reason)                            # type: ignore[arg-type]
        text = bytes(buf).decode("utf-8", errors="replace")
        truncated = overflow.is_set()
        code = proc.returncode
        if not truncated and code not in (0, None) and not text:
            return RunResult(False, "", "error", returncode=code)
        return RunResult(True, text, "", truncated=truncated, returncode=code)
