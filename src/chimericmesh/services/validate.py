"""Pure, total field validators. Nothing here reads the filesystem (``paths.py`` does)."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ValidationFailed
from .models import CLEAR, LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")            # used with fullmatch, never with "$"
RESERVED_IDS = frozenset({"unassigned"})
KNOWN_PROTOCOLS = ("syslog-udp", "syslog-tcp", "telnet", "irc", "file")
DEFAULT_FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
FORMAT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,29}")

_UNWANTED = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


@dataclass(frozen=True, slots=True)
class Limits:
    name: int = 80
    list_len: int = 20
    entry: int = 100
    services: int = 100
    sources_per_service: int = 20
    sources_total: int = 200
    path: int = 1024
    host: int = 100
    format: int = 30
    backfill_max: int = 1000


LIMITS = Limits()

_SERVICE_KEYS = ("id", "name", "hosts", "app_names", "protocols", "code_folder")
_SOURCE_KEYS_CREATE = ("path", "format", "enabled", "host", "backfill_lines")
_SOURCE_KEYS_PATCH = ("format", "enabled", "host", "backfill_lines")


def clean_text(value: str) -> str:
    """Remove control characters and lone surrogates, then strip."""
    return _UNWANTED.sub("", value).strip()


def validate_slug(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationFailed("id", "must be text")
    if SLUG.fullmatch(value) is None:
        raise ValidationFailed("id", "must match [a-z0-9][a-z0-9-]{0,39}")
    if value in RESERVED_IDS:
        raise ValidationFailed("id", f"{value!r} is reserved")
    return value


def _text(field: str, value: object, cap: int) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(field, "must be text")
    cleaned = clean_text(value)
    if not cleaned:
        raise ValidationFailed(field, "must not be empty")
    if len(cleaned) > cap:
        raise ValidationFailed(field, f"must be at most {cap} characters")
    return cleaned


def _entries(field: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationFailed(field, "must be a list of text")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = _text(field, item, LIMITS.entry)
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            out.append(text)
    if len(out) > LIMITS.list_len:
        raise ValidationFailed(field, f"must have at most {LIMITS.list_len} entries")
    return tuple(out)


def _protocols(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationFailed("protocols", "must be a list of text")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in KNOWN_PROTOCOLS:
            raise ValidationFailed("protocols", f"must be one of {', '.join(KNOWN_PROTOCOLS)}")
        if item not in out:
            out.append(item)
    return tuple(out)


def _code_folder(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationFailed("code_folder", "must be a non-empty path")
    return value


def _reject_unknown(raw: Mapping[str, object], allowed: tuple[str, ...]) -> None:
    for key in raw:
        if key not in allowed:
            raise ValidationFailed(str(key)[:40], "unknown or read-only field")


def validate_service_fields(raw: Mapping[str, object], *, for_create: bool) -> ServiceFields | ServicePatch:
    """Create: ``id`` and ``name`` are required. Patch: absent means unchanged, null clears only ``code_folder``."""
    if not isinstance(raw, Mapping):
        raise ValidationFailed("body", "must be an object")
    _reject_unknown(raw, _SERVICE_KEYS if for_create else _SERVICE_KEYS[1:])
    if for_create:
        if "id" not in raw:
            raise ValidationFailed("id", "is required")
        if "name" not in raw:
            raise ValidationFailed("name", "is required")
    values: dict[str, object] = {}
    if "id" in raw:
        values["id"] = validate_slug(raw["id"])
    if "name" in raw:
        values["name"] = _text("name", raw["name"], LIMITS.name)
    for field in ("hosts", "app_names"):
        if field in raw:
            values[field] = _entries(field, raw[field])
    if "protocols" in raw:
        values["protocols"] = _protocols(raw["protocols"])
    if "code_folder" in raw:
        if raw["code_folder"] is None:
            values["code_folder"] = None if for_create else CLEAR
        else:
            values["code_folder"] = _code_folder(raw["code_folder"])
    if for_create:
        return ServiceFields(**values)  # type: ignore[arg-type]
    return ServicePatch(**values)  # type: ignore[arg-type]


def _backfill(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= LIMITS.backfill_max:
        raise ValidationFailed("backfill_lines", f"must be an integer from 0 to {LIMITS.backfill_max}")
    return value


def _format(value: object, known: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ValidationFailed("format", "must be text")
    name = clean_text(value)
    if len(name) > LIMITS.format or name not in known:
        raise ValidationFailed("format", f"must be one of {', '.join(sorted(known))}")
    return name


def validate_log_source_fields(raw: Mapping[str, object], *, known_formats: frozenset[str] | None = None,
                               for_create: bool) -> LogSourceFields | LogSourcePatch:
    """Create: ``path`` and ``format`` are required. Patch: ``path`` cannot change."""
    if not isinstance(raw, Mapping):
        raise ValidationFailed("body", "must be an object")
    known = DEFAULT_FORMATS if known_formats is None else known_formats
    _reject_unknown(raw, _SOURCE_KEYS_CREATE if for_create else _SOURCE_KEYS_PATCH)
    if for_create:
        for field in ("path", "format"):
            if field not in raw:
                raise ValidationFailed(field, "is required")
    values: dict[str, object] = {}
    if "path" in raw:
        path = raw["path"]
        if not isinstance(path, str) or not path:
            raise ValidationFailed("path", "must be a non-empty path")
        values["path"] = path
    if "format" in raw:
        values["format"] = _format(raw["format"], known)
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValidationFailed("enabled", "must be true or false")
        values["enabled"] = raw["enabled"]
    if "host" in raw:
        if raw["host"] is None:
            values["host"] = None if for_create else CLEAR
        else:
            values["host"] = _text("host", raw["host"], LIMITS.host)
    if "backfill_lines" in raw:
        values["backfill_lines"] = _backfill(raw["backfill_lines"])
    if for_create:
        return LogSourceFields(**values)  # type: ignore[arg-type]
    return LogSourcePatch(**values)  # type: ignore[arg-type]
