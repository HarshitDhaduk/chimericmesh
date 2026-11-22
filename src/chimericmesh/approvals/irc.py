"""Pure renderers for ``!rules`` and the replies of ``!revoke``, ``!pause`` and ``!withdraw`` (R8).

Every value that comes from a log or a user is cleaned and cut here, so the reply can never carry a
control character. ``commands.py`` adds the ``[mesh]`` prefix.
"""
from __future__ import annotations

import time
from collections.abc import Sequence

from .format import safe_text
from .models import Rule

MAX_LIST = 5
USAGE_REVOKE = "usage: !revoke <rule id>"
USAGE_WITHDRAW = "usage: !withdraw <proposal id>"
HELP_LINE = "rules: !rules !revoke <rid> !pause !withdraw <pid> (any registered nick)"
NAMES = ("rules", "revoke", "pause", "withdraw")


def rules_lines(rules: Sequence[Rule]) -> list[str]:
    """At most five active rules: ``r1 api-gw signature "<cut to 40>" expires <yyyy-mm-dd>``."""
    active = [r for r in rules if r.status == "active"][:MAX_LIST]
    if not active:
        return ["no active rules"]
    return [f'{safe_text(r.id, 20)} {safe_text(r.service_id, 40)} {safe_text(r.match_type, 12)} '
            f'"{safe_text(r.match_value, 40)}" expires {time.strftime("%Y-%m-%d", time.gmtime(r.expires_at))}'
            for r in active]


def no_rule(rule_id: str) -> str:
    return f"no rule with id {safe_text(rule_id, 30)}"


def no_proposal(proposal_id: str) -> str:
    return f"no proposal with id {safe_text(proposal_id, 30)}"


def revoke_reply(rule_id: str, status_before: str) -> str:
    rid = safe_text(rule_id, 30)
    if status_before in ("revoked", "expired"):
        return f"{rid} is already {status_before}"
    return f"revoked {rid}"


def pause_reply(was_paused: bool) -> str:
    return "auto-approval is already paused" if was_paused else "auto-approval paused"


def withdraw_reply(proposal_id: str, status_before: str) -> str:
    pid = safe_text(proposal_id, 30)
    if status_before != "pending":
        return f"{pid} is already {safe_text(status_before, 12)}, nothing to withdraw"
    return f"withdrawn {pid}"
