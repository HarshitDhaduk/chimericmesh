"""Input validation for rules, proposals and completions. Pure and total: only
``RuleInvalid`` or ``ProposalInvalid`` can escape, never another exception type."""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from typing import Any

from chimericmesh.services.validate import SLUG

from .errors import ProposalInvalid, RuleInvalid
from .format import safe_text
from .limits import MAX_PROPOSALS_PER_SERVICE_HOUR
from .models import (
    CATEGORIES, DEFAULT_EXPIRY_SECONDS, MATCH_TYPES, MAX_EXPIRY_SECONDS, VERIFICATION_RESULTS,
    RuleFields, RulePatch,
)

_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")
NOTE_MAX = 200
MATCH_VALUE_MAX = 200
MAX_FILES_DECLARED = 20
MAX_FILE_CHARS = 300
MAX_VERIFICATION = 300
MAX_ESTIMATED_LINES = 1000
MAX_CHANGED_FILES = 50
MAX_LINES_CHANGED = 1_000_000
FORBIDDEN_RULE_KEYS = frozenset({"created_by", "status", "id", "use_count", "code_folder"})
_FIELD_KEYS = frozenset({"service_id", "match_type", "match_value", "most_urgent_severity", "expires_at",
                         "max_per_hour", "max_per_incident", "max_files", "max_lines", "note"})
_PATCH_KEYS = frozenset({"most_urgent_severity", "expires_at", "max_per_hour", "max_per_incident",
                         "max_files", "max_lines", "note"})


def clean_note(value: object) -> str:
    """Control characters removed, at most 200 characters. Never raises."""
    try:
        text = "" if value is None else str(value)
    except Exception:  # noqa: BLE001
        return ""
    return _CONTROL.sub("", text)[:NOTE_MAX]


def _int(raw: object, field: str, low: int, high: int, error) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise error(field, f"must be an integer from {low} to {high}")
    return raw


def _check_keys(raw: Mapping[str, object], allowed: frozenset[str], error) -> None:
    for key in raw:
        if key in FORBIDDEN_RULE_KEYS:
            raise error(str(key), "is set by the hub and cannot be given")
        if key not in allowed:
            raise error(str(key)[:40], "unknown field")


def _expiry(raw: object, *, now: float, created_at: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise RuleInvalid("expires_at", "must be a finite number of seconds since the epoch")
    if raw <= now:
        raise RuleInvalid("expires_at", "must be in the future")
    if raw > created_at + MAX_EXPIRY_SECONDS:
        raise RuleInvalid("expires_at", "may be at most 30 days after the rule was created")
    return float(raw)


def validate_rule_fields(raw: Mapping[str, object], *, now: float) -> RuleFields:
    try:
        return _rule_fields(raw, now)
    except RuleInvalid:
        raise
    except Exception as exc:  # noqa: BLE001 - total: a surprise is an invalid request
        raise RuleInvalid("body", "could not be read") from exc


def _rule_fields(raw: Mapping[str, object], now: float) -> RuleFields:
    _check_keys(raw, _FIELD_KEYS, RuleInvalid)
    sid = raw.get("service_id")
    if not isinstance(sid, str) or SLUG.fullmatch(sid) is None or sid == "unassigned":
        raise RuleInvalid("service_id", "must be the id of a registered service")
    match_type = raw.get("match_type")
    if match_type not in MATCH_TYPES:
        raise RuleInvalid("match_type", "must be signature or category")
    value = raw.get("match_value")
    if not isinstance(value, str):
        raise RuleInvalid("match_value", "must be text")
    if match_type == "category":
        if value not in CATEGORIES:
            raise RuleInvalid("match_value", "must be one of " + ", ".join(CATEGORIES))
    else:
        if _CONTROL.search(value) or not value.strip():
            raise RuleInvalid("match_value", "must be 1 to 200 characters without control characters")
        if len(value) > MATCH_VALUE_MAX:
            raise RuleInvalid("match_value", "must be 1 to 200 characters")
    expires = raw.get("expires_at")
    return RuleFields(
        service_id=sid, match_type=str(match_type), match_value=value,
        most_urgent_severity=_int(raw.get("most_urgent_severity", 3), "most_urgent_severity", 3, 4, RuleInvalid),
        expires_at=now + DEFAULT_EXPIRY_SECONDS if expires is None else _expiry(expires, now=now, created_at=now),
        max_per_hour=_int(raw.get("max_per_hour", 3), "max_per_hour", 1, MAX_PROPOSALS_PER_SERVICE_HOUR, RuleInvalid),
        max_per_incident=_int(raw.get("max_per_incident", 1), "max_per_incident", 1, 3, RuleInvalid),
        max_files=_int(raw.get("max_files", 5), "max_files", 1, 20, RuleInvalid),
        max_lines=_int(raw.get("max_lines", 100), "max_lines", 1, 500, RuleInvalid),
        note=clean_note(raw.get("note")),
    )


def validate_rule_patch(raw: Mapping[str, object], *, now: float, created_at: float) -> RulePatch:
    try:
        _check_keys(raw, _PATCH_KEYS, RuleInvalid)
        v: dict[str, Any] = {}
        if "most_urgent_severity" in raw:
            v["most_urgent_severity"] = _int(raw["most_urgent_severity"], "most_urgent_severity", 3, 4, RuleInvalid)
        if "max_per_hour" in raw:
            v["max_per_hour"] = _int(raw["max_per_hour"], "max_per_hour", 1, MAX_PROPOSALS_PER_SERVICE_HOUR, RuleInvalid)
        if "max_per_incident" in raw:
            v["max_per_incident"] = _int(raw["max_per_incident"], "max_per_incident", 1, 3, RuleInvalid)
        if "max_files" in raw:
            v["max_files"] = _int(raw["max_files"], "max_files", 1, 20, RuleInvalid)
        if "max_lines" in raw:
            v["max_lines"] = _int(raw["max_lines"], "max_lines", 1, 500, RuleInvalid)
        if "expires_at" in raw:
            v["expires_at"] = _expiry(raw["expires_at"], now=now, created_at=created_at)
        if "note" in raw:
            v["note"] = clean_note(raw["note"])
        return RulePatch(**v)
    except RuleInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuleInvalid("body", "could not be read") from exc


# ---- proposals and completions ---------------------------------------------------------------------

def _is_absolute(name: str) -> bool:
    return (os.path.isabs(name) or name.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:", name)))


def _file_list(raw: object, field: str, limit: int, *, code_folder_given: bool) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise ProposalInvalid(field, "must be a list of file names")
    if len(raw) > limit:
        raise ProposalInvalid(field, f"at most {limit} files")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ProposalInvalid(field, "every entry must be text")
        if len(item) > MAX_FILE_CHARS:
            raise ProposalInvalid(field, f"each entry may have at most {MAX_FILE_CHARS} characters")
        if _CONTROL.search(item):
            raise ProposalInvalid(field, "control characters are not allowed")
        if code_folder_given and field == "files":
            if _is_absolute(item) or ".." in re.split(r"[\\/]", item):
                raise ProposalInvalid(field, "must be relative to the code folder, without ..")
        out.append(item)
    return out


def validate_new_proposal(raw: Mapping[str, object], *, code_folder: str | None) -> dict[str, object]:
    """The normalized ``extra`` of a new proposal: only keys the caller sent."""
    try:
        out: dict[str, object] = {}
        for key in raw:
            if key not in ("kind", "files", "estimated_lines", "verification"):
                raise ProposalInvalid(str(key)[:40], "unknown field")
        if raw.get("kind") is not None:
            if raw["kind"] != "code_change":
                raise ProposalInvalid("kind", "must be code_change")
            out["kind"] = "code_change"
        if raw.get("files") is not None:
            out["files"] = _file_list(raw["files"], "files", MAX_FILES_DECLARED,
                                      code_folder_given=code_folder is not None)
        if raw.get("estimated_lines") is not None:
            out["estimated_lines"] = _int(raw["estimated_lines"], "estimated_lines", 0, MAX_ESTIMATED_LINES,
                                          ProposalInvalid)
        if raw.get("verification") is not None:
            ver = raw["verification"]
            if not isinstance(ver, str) or len(ver) > MAX_VERIFICATION:
                raise ProposalInvalid("verification", f"must be text of at most {MAX_VERIFICATION} characters")
            out["verification"] = safe_text(ver, MAX_VERIFICATION)
        return out
    except ProposalInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProposalInvalid("body", "could not be read") from exc


def validate_completion(raw: Mapping[str, object] | None) -> dict[str, object]:
    try:
        raw = raw or {}
        out: dict[str, object] = {}
        for key in raw:
            if key not in ("files_changed", "lines_changed", "verification_result"):
                raise ProposalInvalid(str(key)[:40], "unknown field")
        if raw.get("files_changed") is not None:
            out["files_changed"] = _file_list(raw["files_changed"], "files_changed", MAX_CHANGED_FILES,
                                              code_folder_given=False)
        if raw.get("lines_changed") is not None:
            out["lines_changed"] = _int(raw["lines_changed"], "lines_changed", 0, MAX_LINES_CHANGED, ProposalInvalid)
        if raw.get("verification_result") is not None:
            if raw["verification_result"] not in VERIFICATION_RESULTS:
                raise ProposalInvalid("verification_result", "must be passed, failed or not_run")
            out["verification_result"] = raw["verification_result"]
        return out
    except ProposalInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProposalInvalid("body", "could not be read") from exc


__all__ = ["clean_note", "validate_completion", "validate_new_proposal", "validate_rule_fields",
           "validate_rule_patch"]
