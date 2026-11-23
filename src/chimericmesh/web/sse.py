"""Pure SSE framing and resume-id parsing (stdlib and core only).

Every function is total except ``frame_named`` (a fixed name set) and ``parse_after`` (a bad value is
a client error). The payload is ASCII JSON on one line, so a message cannot add a field to a frame.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from chimericmesh.events import Event

MAX_MESSAGE_CHARS = 4096
_MAX_NAME_CHARS = 255
_MAX_ID = 2**53
_ID = re.compile(r"[0-9]{1,16}", re.ASCII)


def _text(value: object, limit: int) -> str:
    s = value if isinstance(value, str) else str(value)
    return s[:limit].encode("utf-8", "replace").decode("utf-8")


def event_payload(event: Event) -> str:
    """One line of ASCII JSON with the ten stream keys; ``raw`` is never sent."""
    obj = {
        "id": int(event.id),
        "ts": float(event.ts),
        "source": _text(event.source, _MAX_NAME_CHARS),
        "host": _text(event.host, _MAX_NAME_CHARS),
        "app": _text(event.app, _MAX_NAME_CHARS),
        "severity": int(event.severity),
        "severity_name": event.severity_name,
        "message": _text(event.message, MAX_MESSAGE_CHARS),
        "service_id": None if event.service_id is None else _text(event.service_id, _MAX_NAME_CHARS),
        "log_source_id": None if event.log_source_id is None else _text(event.log_source_id, _MAX_NAME_CHARS),
    }
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def frame_event(event_id: int, payload: str) -> str:
    return f"id: {int(event_id)}\ndata: {payload}\n\n"


def frame_named(name: Literal["dropped", "closed"], payload: str) -> str:
    if name not in ("dropped", "closed"):
        raise ValueError(f"unknown event name: {name!r}")
    return f"event: {name}\ndata: {payload}\n\n"


def frame_comment(text: str = "hb") -> str:
    return ": " + str(text).replace("\r", "").replace("\n", " ") + "\n\n"


def _parse_id(value: str | None) -> int | None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        return None
    n = int(value)
    return n if n <= _MAX_ID else None


def parse_last_event_id(header: str | None) -> int | None:
    """Total: ``None`` for anything that is not 1 to 16 ASCII digits no larger than 2**53."""
    return _parse_id(header)


def parse_after(value: str | None) -> int | None:
    if value is None:
        return None
    n = _parse_id(value)
    if n is None:
        raise ValueError("after must be a non-negative integer")
    return n
