"""Limits and safe text of the agent loop (R1.7). Pure and total; stdlib only.

Log text is untrusted. ``clean_text`` and ``cap_items`` are applied in the service layer, so every client
of a route gets stripped and capped text, not only Kiro.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping

log = logging.getLogger(__name__)

MAX_FIELD = 500                 # characters per log-derived string field (R1.7)
MAX_RESPONSE_BYTES = 20_480     # per response (R1.7)
MAX_EVENTS, DEFAULT_EVENTS = 100, 25
MAX_MINUTES = 240
MAX_QUERY = 100
MAX_HITS = 10
MAX_FILES_SCANNED = 2000
MAX_FILE_BYTES = 512 * 1024
SEARCH_SECONDS = 5.0
WAIT_MIN, TICK, MAX_WAITERS = 1, 0.5, 4
WAIT_CEILING = 25               # hard ceiling; configuration can only lower the wait
MAX_EXCLUDE, MAX_ID_LEN = 200, 40
RULE_APPROVAL_WINDOW = 1800     # equals ApprovalSettings.rule_approval_ttl; a test asserts it

_ENV_WAIT = "CHIMERICMESH_WATCH_MAX_WAIT"
_UNSAFE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")


def clean_text(value: object, limit: int = MAX_FIELD) -> str:
    """``str(value)`` without control characters or lone surrogates, cut to ``limit`` characters."""
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001 - total by contract
        return ""
    if limit <= 0:
        return ""
    return _UNSAFE.sub("", text)[:limit]


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _size(envelope: Mapping[str, object]) -> int:
    return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace"))


def cap_items(envelope: dict, key: str, items: list[dict], *, max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """Put the longest prefix of ``items`` under ``envelope[key]`` that keeps the JSON at ``max_bytes`` or
    less. ``truncated`` is set to True only when items were dropped. An empty list always fits."""
    whole = dict(envelope, **{key: list(items)})
    if _size(whole) <= max_bytes:
        return whole
    low, high = 0, len(items)           # the largest prefix length that fits lies in [low, high]
    while low < high:
        mid = (low + high + 1) // 2
        if _size(dict(envelope, **{key: items[:mid], "truncated": True})) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return dict(envelope, **{key: list(items[:low]), "truncated": True})


def watch_max_wait(environ: Mapping[str, str] | None = None) -> int:
    """The longest ``wait_for_work`` hold in seconds. The setting can only lower the ceiling (R1-M6)."""
    env = os.environ if environ is None else environ
    raw = env.get(_ENV_WAIT)
    if raw is None or not str(raw).strip():
        return WAIT_CEILING
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning("%s is not an integer; using %s", _ENV_WAIT, WAIT_CEILING)
        return WAIT_CEILING
    return clamp(value, WAIT_MIN, WAIT_CEILING)


WAIT_MAX = watch_max_wait()
