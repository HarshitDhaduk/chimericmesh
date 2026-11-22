"""Verify: may the agent edit these files for this proposal right now? (R3.1, R3.2). Pure and total.

The six checks are the sub-criteria 3.1a to 3.1f of the design (review L8). ``ok`` is true exactly when none
of 3.1a to 3.1e and 3.1f fails (3.1e compares nothing with a declared list, but since task 4.4 it refuses a
protected name such as ``.env`` for a user approval without declared files); the note of 3.1f does not make
``ok`` false. The
function reads the proposal column ``approved_by_rule`` and never the ``decided_by`` text, so an IRC nick
such as ``rule:r1`` cannot pass as a rule approval. Reasons are fixed texts: they never contain a file name,
an action, a signature or any other log-derived text.
"""
from __future__ import annotations

import math
import ntpath
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from chimericmesh.approvals.scope import PROTECTED_PATTERNS, check_file, glob_to_regex
from chimericmesh.services.paths import path_in_code_folder

from .limits import RULE_APPROVAL_WINDOW, clean_text

MAX_FILES = 20
MAX_PATH = 300
_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")
_PROTECTED = tuple(glob_to_regex(p) for p in PROTECTED_PATTERNS)
_STATUS_WORDS = ("pending", "approved", "denied", "done")
NO_FOLDER_NOTE = "no code folder, path scope not checked"
FAILSAFE = "the proposal could not be verified"


@dataclass(frozen=True, slots=True)
class VerifyInputs:
    proposal_id: str
    status: str
    approved_by_rule: str | None
    declared_files: tuple[str, ...] | None
    approval: Mapping[str, object] | None        # from ``approvals.approval_of``
    rule_status: str | None                      # None when the rule is unknown
    rule_code_folder: str | None
    rule_expires_at: float | None
    paused: bool
    approvals_available: bool
    code_folder: str | None                      # the live folder of the incident's service
    has_service: bool
    now: float
    protected_dirs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    reasons: tuple[str, ...]
    status: str
    approval: Mapping[str, object] | None
    declared_files: tuple[str, ...]


def _is_abs(path: str) -> bool:
    return os.path.isabs(path) or ntpath.isabs(path)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_PATH and not _CONTROL.search(value)


def normalize_file(code_folder: str | None, raw: str, *,
                   normcase: Callable[[str], str] = os.path.normcase) -> str | None:
    """A comparison key: absolute, ``normpath`` and ``normcase``. A relative entry is joined to the code
    folder (the auto-approval convention for *declared* files). ``None`` for an empty or invalid string, and
    for a relative entry when there is no code folder."""
    if not _valid_text(raw):
        return None
    if _is_abs(raw):
        return normcase(os.path.normpath(raw))
    if isinstance(code_folder, str) and code_folder:
        return normcase(os.path.normpath(os.path.join(code_folder, raw)))
    return None


def samefolder(a: str | None, b: str | None, *, normcase: Callable[[str], str] = os.path.normcase) -> bool:
    """Same folder after ``normpath`` (which drops a trailing separator) and ``normcase``."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return normcase(os.path.normpath(a)) == normcase(os.path.normpath(b))


def _status_reason(status: object) -> str | None:
    if status == "approved":
        return None
    word = status if status in _STATUS_WORDS else "unknown"
    return f"proposal status is {word}, not approved"


def _rule_reason(inp: VerifyInputs, normcase: Callable[[str], str]) -> str | None:
    """3.1b: None when a user approval or a rule approval that is still valid, else the first failing part."""
    if inp.approved_by_rule is None:
        return None
    rid = clean_text(inp.approved_by_rule, 40)
    if not inp.approvals_available or not isinstance(inp.rule_status, str):
        return f"rule {rid} is unknown"
    if inp.rule_status != "active":
        return f"rule {rid} is {clean_text(inp.rule_status, 20)}"
    if inp.paused:
        return "auto-approval is paused"
    if not _number(inp.now) or not _number(inp.rule_expires_at) or not inp.now < inp.rule_expires_at:
        return f"rule {rid} has expired"
    if not samefolder(inp.rule_code_folder, inp.code_folder, normcase=normcase):
        return f"rule {rid} was bound to another code folder"
    approved_at = inp.approval.get("approved_at") if isinstance(inp.approval, Mapping) else None
    if not _number(approved_at):
        return "rule approval time is unknown"
    if not inp.now - approved_at <= RULE_APPROVAL_WINDOW:
        return "rule approval is older than 30 minutes"
    return None


def _protected_name(folder: str, path: str, realpath: Callable[[str], str],
                    normcase: Callable[[str], str]) -> bool:
    """True when ``path`` (as written, or after resolving links) lies in ``folder`` under a protected pattern.
    Only the ``PROTECTED_PATTERNS`` part of ``check_file`` runs here: never the extension rule (open item O13)."""
    pairs = ((normcase(path), normcase(folder)), (normcase(realpath(path)), normcase(realpath(folder))))
    for target, base in pairs:
        try:
            rel = os.path.relpath(target, base).replace(os.sep, "/")
        except ValueError:                      # another drive: not inside the folder, 3.1f reports it
            continue
        if rel != ".." and not rel.startswith("../") and any(rx.match(rel) for rx in _PROTECTED):
            return True
    return False

def _files_reason(files: object) -> tuple[str | None, list[tuple[int, str]]]:
    """3.1c: the reason (or None) and the valid entries (1-based index, path) of the first 20."""
    if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= MAX_FILES:
        reason = "files must list 1 to 20 paths"
        entries = list(files)[:MAX_FILES] if isinstance(files, (list, tuple)) else []
    else:
        reason, entries = None, list(files)
    valid: list[tuple[int, str]] = []
    for n, entry in enumerate(entries, start=1):
        if not _valid_text(entry):
            reason = reason or f"file {n} is not a valid path"
        elif not _is_abs(entry):
            reason = reason or f"file {n} must be an absolute path"
        else:
            valid.append((n, entry))
    return reason, valid


def evaluate_verify(inp: VerifyInputs, files: Sequence[str], *,
                    realpath: Callable[[str], str] = os.path.realpath,
                    normcase: Callable[[str], str] = os.path.normcase) -> VerifyResult:
    """Run 3.1a to 3.1f. One reason per failed check, in that order. Never raises."""
    try:
        return _evaluate(inp, files, realpath, normcase)
    except Exception:  # noqa: BLE001 - total by contract: any surprise is a refusal
        return VerifyResult(False, (FAILSAFE,), "unknown", None, ())


def _evaluate(inp: VerifyInputs, files: object, realpath: Callable[[str], str],
              normcase: Callable[[str], str]) -> VerifyResult:
    reasons: list[str] = []
    folder = inp.code_folder if isinstance(inp.code_folder, str) and inp.code_folder else None
    declared = inp.declared_files
    if declared is None and inp.approved_by_rule is not None:
        declared = ()                                   # a rule approval always carries a declared list
    declared_view = tuple(d for d in declared if isinstance(d, str)) if isinstance(declared, (list, tuple)) else ()

    for reason in (_status_reason(inp.status), _rule_reason(inp, normcase)):                       # 3.1a, 3.1b
        if reason is not None:
            reasons.append(reason)
    files_reason, valid = _files_reason(files)                                                      # 3.1c
    if files_reason is not None:
        reasons.append(files_reason)

    if declared is not None:                                                                        # 3.1d (3.1e: none)
        keys = {k for k in (normalize_file(folder, d, normcase=normcase) for d in declared_view) if k is not None}
        protected = tuple(p for p in (inp.protected_dirs or ()) if isinstance(p, str))
        for n, path in valid:
            key = normalize_file(folder, path, normcase=normcase)
            if key is None or key not in keys:
                reasons.append(f"file {n} was not declared in the proposal")
                break
            if folder is not None and check_file(folder, path, protected_dirs=protected, realpath=realpath,
                                                 normcase=normcase) is not None:
                reasons.append(f"file {n} is outside the allowed scope")
                break

    elif folder is not None:                                                                        # 3.1e (O13)
        for n, path in valid:
            if _protected_name(folder, path, realpath, normcase):
                reasons.append(f"file {n} is a protected file")
                break

    note: str | None = None
    if folder is None:                                                                              # 3.1f
        note = NO_FOLDER_NOTE
    else:
        for n, path in valid:
            if not path_in_code_folder(folder, path, realpath=realpath, normcase=normcase):
                reasons.append(f"file {n} is outside the code folder")
                break

    status = inp.status if inp.status in _STATUS_WORDS else "unknown"
    approval = dict(inp.approval) if isinstance(inp.approval, Mapping) else None
    ok = not reasons
    if note is not None:
        reasons.append(note)
    return VerifyResult(ok, tuple(reasons), status, approval, declared_view)
