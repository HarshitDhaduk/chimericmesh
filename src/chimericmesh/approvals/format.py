"""Announcement text and audit detail builders. Pure and total: nothing here raises on any input."""
from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACES = re.compile(r" {2,}")
_SURROGATE = re.compile("[\ud800-\udfff]")
MAX_LINE_BYTES = 400


def safe_text(value: object, limit: int) -> str:
    """Remove control characters and lone surrogates, collapse spaces, cut to ``limit`` characters."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return ""
    text = _SURROGATE.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return text[:max(0, limit)].rstrip()


def _cut_bytes(text: str, limit: int) -> str:
    """Cut to at most ``limit`` UTF-8 bytes without splitting a character."""
    raw = text.encode("utf-8", "ignore")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def auto_approved_line(pid: str, rid: str, sid: str, action: str) -> str:
    """``AUTO-APPROVED p3 by rule r1 (service api-gw): <action>. Revoke with !revoke r1``.

    Never says "reply !approve": nobody has to answer. At most 400 UTF-8 bytes.
    """
    head = f"AUTO-APPROVED {safe_text(pid, 20)} by rule {safe_text(rid, 20)} (service {safe_text(sid, 40)}): "
    tail = f". Revoke with !revoke {safe_text(rid, 20)}"
    room = MAX_LINE_BYTES - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    body = _cut_bytes(safe_text(action, 200), max(0, room)).rstrip()
    return head + body + tail


def cooldown_note(prior_pid: str, rule_id: str) -> str:
    return (f" (rule {safe_text(rule_id, 20)} did not auto-approve it: the earlier fix "
            f"{safe_text(prior_pid, 20)} was completed shortly before this reopen)")


def rule_line(verb: str, rid: str, sid: str, actor: str, extra: str = "") -> str:
    """One announcement for a rule or proposal event. ``actor`` and ``extra`` are cleaned and cut."""
    who = safe_text(actor, 40) or "unknown"
    more = safe_text(extra, 120)
    rid, sid = safe_text(rid, 20), safe_text(sid, 40)
    if verb == "created":
        text = f"RULE {rid} created by {who} for {sid}"
    elif verb == "revoked":
        text = f"RULE {rid} revoked by {who}"
    elif verb == "rule_paused":
        text = f"RULE {rid} paused by {who}"
    elif verb == "enabled":
        text = f"RULE {rid} re-enabled by {who}"
    elif verb == "suspended":
        text = f"RULE {rid} suspended by {who}: {more}" if more else f"RULE {rid} suspended by {who}"
    elif verb == "expired":
        text = f"RULE {rid} expired ({who})"
    elif verb == "paused":
        text = f"AUTO-APPROVAL paused by {who}"
    elif verb == "resumed":
        text = f"AUTO-APPROVAL resumed by {who}"
    elif verb == "withdrawn":
        text = f"PROPOSAL {rid} withdrawn by {who}"
    elif verb == "denied":   # rid is the proposal id, sid the rule id
        text = f"PROPOSAL {rid} denied by {who}: rule {sid} {more or 'is no longer active'}"
    else:
        text = f"RULE {rid} {safe_text(verb, 20)} by {who}"
    return _cut_bytes(text, MAX_LINE_BYTES)
