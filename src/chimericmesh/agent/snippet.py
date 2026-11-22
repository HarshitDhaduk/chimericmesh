"""The permissions snippet (R5.4 to R5.8). Pure text; nothing here writes a Kiro file (F5).
The text is YAML with leading comments. Every value that comes from outside (service id, folder, workspace
root, test command) is cleaned and quoted with ``json.dumps``, so it cannot inject a rule or a line.
"""
from __future__ import annotations
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from chimericmesh.approvals.scope import PROTECTED_PATTERNS
from chimericmesh.services.paths import path_in_code_folder
from .errors import InvalidQuery, NoCodeFolder
from .limits import clean_text
log = logging.getLogger(__name__)
ENV_ROOT = "CHIMERICMESH_WORKSPACE_ROOT"
DEFAULT_TEST_COMMAND = "python -m pytest -q"
STATE_FILE = ".chimericmesh-active-proposal"
_COMMAND = re.compile(r"[A-Za-z0-9_.:/ -]{1,80}")
_BAD_WORDS = frozenset({"curl", "wget", "nc", "ncat", "powershell", "pwsh", "bash", "sh", "cmd"})
_SHORT_C = re.compile(r"^-[A-Za-z0-9]*c")          # -c, -qc, -Sc: inline code or an alternative config
_GLOB_CHARS = frozenset("*?[]{}")
def validate_test_command(value: str) -> str:
    """The one command the snippet may show as a shell example. Raises ``InvalidQuery``."""
    if not isinstance(value, str):
        raise InvalidQuery("test_command must be text")
    text = value.strip()
    if not text or not _COMMAND.fullmatch(text):
        raise InvalidQuery("test_command must be 1 to 80 characters of letters, digits and _ . : / - or space")
    for token in text.split():
        base = re.split(r"[\\/]", token)[-1].lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base in _BAD_WORDS or _SHORT_C.match(token):
            raise InvalidQuery("test_command contains a command that is not allowed")
    return text
def find_workspace_root(environ: Mapping[str, str], cwd: str, *,
                        exists: Callable[[str], bool] = os.path.isdir) -> str | None:
    """``CHIMERICMESH_WORKSPACE_ROOT`` when it is an existing absolute directory; ``None`` (workspace unknown,
    no allow line) with one warning when it is set but invalid; else the nearest ancestor of ``cwd`` that has a
    ``.kiro`` directory (R1-M3); else ``cwd``."""
    raw = environ.get(ENV_ROOT)
    if raw is not None and str(raw).strip():
        value = str(raw).strip()
        try:
            valid = os.path.isabs(value) and bool(exists(value))
        except (OSError, ValueError):
            valid = False
        if valid:
            return value
        log.warning("%s is not an existing absolute directory; the permissions snippet has no allow line", ENV_ROOT)
        return None
    here = cwd
    while True:
        try:
            if exists(os.path.join(here, ".kiro")):
                return here
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(here)
        if not parent or parent == here:
            return cwd
        here = parent
def _text(value: object, limit: int = 300) -> str:
    """Printable text only (no control, no line separator), cut to ``limit``."""
    return "".join(c for c in clean_text(value, limit) if c.isprintable())
def _quoted(value: object, limit: int = 300) -> str:
    return json.dumps(_text(value, limit), ensure_ascii=False)
def _relative_allow(folder: str, workspace_root: str | None) -> str | None:
    """The workspace-relative glob for ``folder``, or ``None`` when no safe allow line exists."""
    if not workspace_root or not isinstance(workspace_root, str) or not isinstance(folder, str):
        return None
    if not path_in_code_folder(workspace_root, folder):              # strict descendant only
        return None
    try:
        rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(workspace_root))
    except (OSError, ValueError):
        return None
    rel = rel.replace(os.sep, "/")
    if (not rel or rel == "." or rel.startswith(("/", "~", "!", "../")) or ".." in rel.split("/")
            or "\\" in rel or len(rel) > 200 or any(c in _GLOB_CHARS for c in rel) or not rel.isprintable()
            or re.match(r"^[A-Za-z]:", rel)):
        return None
    return rel + "/**"
def build_snippet(service, *, workspace_root: str | None, test_command: str = DEFAULT_TEST_COMMAND) -> str:
    """The text for ``service`` (anything with ``id`` and ``code_folder``). Raises ``NoCodeFolder`` when the
    service has no folder and ``InvalidQuery`` for an unsafe ``test_command``."""
    folder = getattr(service, "code_folder", None)
    if not folder:
        raise NoCodeFolder("service has no code folder")
    command = validate_test_command(test_command)
    allow = _relative_allow(folder, workspace_root)
    lines = [
        "# Mode: AUTONOMOUS (hands-off). This file is a standing allow for edits inside one code folder.",
        "# Prompt mode is the default (no snippet): do not paste this file unless you want hands-off edits.",
        "# Trade-off: edits inside the folder no longer prompt, including edits for a proposal that is still pending.",
        "# The gate then rests on the prompt text, verify_proposal and the audit trail, which a prompt-injected",
        "# agent could ignore. Everything outside the folder keeps asking.",
        "#",
        "# Where to put it: the user-scoped file ~/.kiro/settings/permissions.yaml applies to",
        "# every Kiro session and agent of this OS user, not only mesh-watcher. The workspace-scoped file",
        "# ~/.kiro/workspace-roots/<hash>/permissions.yaml limits the rule to this workspace and is preferred",
        "# (the hash must be looked up on your machine).",
        "# A relative deny glob in the user-scoped file may apply in every workspace of this user, so put the",
        "# deny lines in the workspace-scoped file.",
        "#",
        "# Unverified schema: the layout below is modelled on the permissions.rules shape of the agent profile.",
        "# Check https://kiro.dev/docs/permissions/ for the permissions.yaml schema and the shell match rule",
        "# (prefix or exact) and verify before pasting (date of manual check: not done yet).",
        "# Globs are workspace-relative; whether the ~/ and relative forms match is a check live in the IDE.",
        f"# Workspace root used: {_quoted(workspace_root) if workspace_root else '(unknown)'}",
        f"# Service: {_quoted(getattr(service, 'id', ''), 80)}",
        "rules:",
    ]
    if allow is not None:
        lines += ["  - capability: fs_write", "    effect: allow", f"    match: [{json.dumps(allow, ensure_ascii=False)}]"]
    else:
        lines += ["  # Code folder is outside the open workspace (or equals it, or has a glob character in its path).",
                  "  # The fix still runs, Kiro prompts for each write. No allow line is emitted."]
    lines += ["  - capability: fs_write", "    effect: deny",
              "    match: [" + ", ".join(json.dumps(p) for p in PROTECTED_PATTERNS) + "]",
              "  # Optional: exactly one test command. It runs code the agent has just written, with your rights.",
              "  # Remove the leading # to enable it. No wildcard, no shell metacharacters.",
              "#  - capability: shell",
              f"#    match: [{json.dumps(command)}]",
              "#    effect: allow",
              "  # Optional, only if you installed the mesh-verify-write hook: the one state file it reads.",
              "#  - capability: fs_write",
              f"#    match: [{json.dumps(STATE_FILE)}]",
              "#    effect: allow",
              "# Everything else keeps asking: other folders, other files, every other shell command.",
              ""]
    return "\n".join(lines)
