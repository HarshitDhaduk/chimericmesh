"""IRC operator commands (``!status``, ``!approve p1`` ...).

``parse_command`` is a pure function that never raises. ``handle_command`` runs a parsed
command against the hub and sends reply lines through ``hub.say``, which reaches IRC via
``format_privmsg`` (strips CR/LF/NUL, splits long lines).

SAFETY: approving a proposal only records the human decision and announces it. Nothing
here (or anywhere in ChimericMesh) executes a proposed action on any host.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .events import SEVERITY_NAMES
from .hub import Hub
from .store.errors import StoreError

log = logging.getLogger("chimericmesh.commands")

MAX_COMMAND_CHARS = 200
MAX_ARGS = 5
MAX_NAME = 32
MAX_LIST = 5
MAX_STEPS = 4
PREFIX = "[mesh]"
_CTRL = re.compile(r"[\r\n\x00]+")

HELP_LINES = [
    "commands: !help !status !incidents !explain <id> !proposals",
    "operators only: !resolve <id> [note] !approve <pid> !deny <pid>",
]


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()


def parse_command(text: str) -> Command | None:
    """Parse ``!name arg ...``. Returns None when text is not a command. Never raises."""
    if not isinstance(text, str) or not text.startswith("!"):
        return None
    parts = text[:MAX_COMMAND_CHARS].split()
    if not parts:
        return None
    name = parts[0][1:MAX_NAME + 1].lower()
    if not name:
        return None
    return Command(name=name, args=tuple(parts[1:1 + MAX_ARGS]))


def _clean(text: str, limit: int = 80) -> str:
    return _CTRL.sub(" ", text)[:limit]


def _line(text: str) -> str:
    return f"{PREFIX} {_CTRL.sub(' ', text)}"


async def handle_command(hub: Hub, nick: str, channel: str, cmd: Command) -> list[str]:
    """Execute a command and post its replies to the channel. Returns the reply lines."""
    hub.commands_run += 1
    try:
        lines = await _run(hub, nick, cmd)
    except StoreError:
        log.error("store unavailable while running !%s", cmd.name)
        lines = ["store unavailable, try again"]
    replies = [_line(t) for t in lines]
    target = channel if channel.startswith("#") else hub.irc_channel
    for reply in replies:
        await hub.say(target, reply)
    return replies


async def _run(hub: Hub, nick: str, cmd: Command) -> list[str]:
    name, args = cmd.name, cmd.args
    if name == "help":
        return list(HELP_LINES)
    if name == "status":
        return [_status(hub)]
    if name == "incidents":
        return _incidents(hub)
    if name == "explain":
        return _explain(hub, args)
    if name == "proposals":
        return _proposals(hub)
    if name in {"resolve", "approve", "deny"}:
        if not hub.is_operator(nick):
            return [f"sorry {_clean(nick, 30)}, only operators may use !{name}"]
        if name == "resolve":
            return await _resolve(hub, nick, args)
        return await _decide(hub, nick, name == "approve", args)
    return [f"unknown command !{_clean(name, 32)}, try !help"]


def _status(hub: Hub) -> str:
    open_n = sum(1 for i in hub.incidents() if not i.resolved)
    pending = len(hub.list_proposals("pending"))
    return f"events seen: {len(hub.events)}, open incidents: {open_n}, pending proposals: {pending}"


def _incidents(hub: Hub) -> list[str]:
    open_incs = [i for i in hub.incidents() if not i.resolved][:MAX_LIST]
    if not open_incs:
        return ["no open incidents"]
    return [f"{i.id} {i.category} {_clean(i.host, 40)} x{i.count} {SEVERITY_NAMES[i.severity]}"
            for i in open_incs]


def _explain(hub: Hub, args: tuple[str, ...]) -> list[str]:
    if not args:
        return ["usage: !explain <incident id>"]
    inc = hub.get_incident(args[0])
    if inc is None:
        return [f"no incident with id {_clean(args[0], 30)}, try !incidents"]
    lines = [f"{inc.id}: {_clean(inc.hypothesis, 300)}"]
    lines += [f"{n}. {_clean(step, 200)}" for n, step in enumerate(inc.remediation[:MAX_STEPS], 1)]
    return lines


def _proposals(hub: Hub) -> list[str]:
    pending = hub.list_proposals("pending")[:MAX_LIST]
    if not pending:
        return ["no pending proposals"]
    return [f"{p.id} for {p.incident_id}: {_clean(p.action, 120)}" for p in pending]


async def _resolve(hub: Hub, nick: str, args: tuple[str, ...]) -> list[str]:
    if not args:
        return ["usage: !resolve <incident id> [note]"]
    note = f"by {_clean(nick, 30)}: {' '.join(args[1:])}" if len(args) > 1 else f"by {_clean(nick, 30)}"
    inc = await hub.resolve(args[0], _clean(note, 300))
    if inc is None:
        return [f"no incident with id {_clean(args[0], 30)}, try !incidents"]
    return [f"resolved {inc.id}"]


async def _decide(hub: Hub, nick: str, approve: bool, args: tuple[str, ...]) -> list[str]:
    verb = "approve" if approve else "deny"
    if not args:
        return [f"usage: !{verb} <proposal id>"]
    pid = args[0].lower()
    if hub.get_proposal(pid) is None:
        return [f"no proposal with id {_clean(pid, 30)}, try !proposals"]
    prop = await hub.decide(pid, approve, nick)
    if prop is None:
        return [f"{_clean(pid, 30)} is not pending, nothing changed"]
    return [f"{prop.id} {prop.status}"]
