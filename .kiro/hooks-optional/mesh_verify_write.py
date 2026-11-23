"""Optional PreToolUse hook: block a write unless the hub's verify route says it is allowed (agent-loop R5.9).

Opt-in extra layer. Copy this file and mesh-verify-write.json from .kiro/hooks-optional/ to .kiro/hooks/ to
install it; delete both copies to remove it. While it is installed every write in the workspace needs an
active proposal named in .chimericmesh-active-proposal (workspace root).

Reads the hook JSON from stdin. Exit 2 (block, reason on stderr) when a state file is missing or bad, the hub
URL is not a loopback http URL, the hub is unreachable or does not answer ``ok: true``, or verify notes that
it could not check the path scope. Exit 0 when stdin is not JSON or names no path, when the only target is the
state file, or when the hub answers ``ok: true``. The script sends no token, reads no token file and talks to
loopback only. Stdlib only; 5 s timeout.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

STATE_FILE = ".chimericmesh-active-proposal"
DEFAULT_URL = "http://127.0.0.1:8000"
NO_FOLDER_NOTE = "no code folder, path scope not checked"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
PROPOSAL_ID = re.compile(r"p[1-9][0-9]*")
TIMEOUT = 5.0
MAX_STATE_BYTES = 64
MAX_TARGETS = 20
PATH_KEYS = ("path", "file_path")


def targets_of(data) -> list[str]:
    """Target paths found in ``tool_input`` (keys path, file_path, paths); text without a control character."""
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    if not isinstance(tool_input, dict):
        return []
    found = [tool_input.get(key) for key in PATH_KEYS]
    paths = tool_input.get("paths")
    if isinstance(paths, list):
        found.extend(paths)
    return [p for p in found if isinstance(p, str) and p and not re.search(r"[\x00-\x1f\x7f]", p)]


def base_url(env) -> str | None:
    """The hub base URL from ``CHIMERICMESH_URL``, or ``None`` unless it is plain http to a loopback host."""
    raw = env.get("CHIMERICMESH_URL") or DEFAULT_URL
    try:
        parts = urllib.parse.urlsplit(raw.strip())
        host = parts.hostname
        port = parts.port
        if (parts.scheme != "http" or host not in LOOPBACK or parts.username is not None
                or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
            return None
    except ValueError:
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}" + (f":{port}" if port is not None else "")


def read_state(cwd: str) -> str | None:
    """The proposal id in the state file, or ``None`` when it is missing or not ``p<n>``."""
    try:
        with open(os.path.join(cwd, STATE_FILE), "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_STATE_BYTES:
        return None
    text = raw.decode("utf-8", "replace").strip()
    return text if PROPOSAL_ID.fullmatch(text) else None


def decide(stdin_text, *, cwd, env, post):
    """Return ``(exit code, stderr text)``. ``post(url, body, timeout)`` returns the parsed JSON reply or raises."""
    try:
        data = json.loads(stdin_text)
    except (ValueError, TypeError, RecursionError):
        return 0, ""
    raw_targets = targets_of(data)
    if not raw_targets:
        return 0, ""
    state_path = os.path.normcase(os.path.abspath(os.path.join(cwd, STATE_FILE)))
    files = []
    for raw in raw_targets[:MAX_TARGETS]:
        full = os.path.abspath(os.path.join(cwd, raw))
        if os.path.normcase(full) != state_path:
            files.append(full)
    if not files:
        return 0, ""
    pid = read_state(cwd)
    if pid is None:
        return 2, f"Blocked: {STATE_FILE} is missing or does not hold one proposal id like p4. No write without an active proposal.\n"
    base = base_url(env)
    if base is None:
        return 2, "Blocked: CHIMERICMESH_URL must be http://127.0.0.1, http://localhost or http://[::1] (loopback only).\n"
    try:
        reply = post(f"{base}/api/proposals/{pid}/verify", json.dumps({"files": files}).encode("utf-8"), TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - any failure to ask the hub blocks the write
        return 2, f"Blocked: the hub could not verify proposal {pid} ({type(exc).__name__}).\n"
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        reasons = reply.get("reasons") if isinstance(reply, dict) else None
        first = reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], str) else "verify said no"
        return 2, f"Blocked by verify for proposal {pid}: {first[:300]}\n"
    reasons = reply.get("reasons")
    if isinstance(reasons, list) and NO_FOLDER_NOTE in reasons:
        return 2, f"Blocked: {NO_FOLDER_NOTE} (proposal {pid}).\n"
    return 0, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_post(url: str, body: bytes, timeout: float):
    """POST ``body`` as JSON, no token, no redirect; raises on any error or a reply that is not JSON."""
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def main() -> int:
    try:
        text = sys.stdin.read()
    except Exception:  # noqa: BLE001 - a hook that cannot read its input must not break the session
        return 0
    try:
        code, message = decide(text, cwd=os.getcwd(), env=os.environ, post=http_post)
    except Exception:  # noqa: BLE001 - a bug in this script fails open; Kiro's own prompt still applies
        return 0
    if message:
        sys.stderr.write(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
