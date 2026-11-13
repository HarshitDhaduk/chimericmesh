"""Outbound notifications: modern webhooks and legacy IRC announcements."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .diagnostics import Incident
from .events import SEVERITY_NAMES


def webhook_payload(incident: Incident, event_type: str = "incident.opened") -> dict:
    return {
        "type": event_type,
        "incident": incident.to_dict(),
        "severity_name": SEVERITY_NAMES[incident.severity],
        "text": irc_summary(incident),
    }


def irc_summary(incident: Incident) -> str:
    return (
        f"[{SEVERITY_NAMES[incident.severity].upper()}] {incident.category} on {incident.host} "
        f"x{incident.count} (id {incident.id}): {incident.hypothesis}"
    )


def is_safe_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


async def send_webhook(url: str, payload: dict, client: httpx.AsyncClient | None = None) -> bool:
    """POST JSON to ``url``. Returns True on 2xx. Never raises on network errors."""
    if not is_safe_webhook_url(url):
        return False
    owns = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await client.post(url, json=payload)
        return resp.is_success
    except httpx.HTTPError:
        return False
    finally:
        if owns:
            await client.aclose()
