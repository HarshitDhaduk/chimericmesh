"""Property tests: command parsing never raises, replies are injection-safe, proposals stay valid."""
import asyncio
import re

from hypothesis import given, settings, strategies as st

from chimericmesh.commands import Command, handle_command, parse_command
from chimericmesh.hub import PROPOSAL_STATUSES, Hub
from chimericmesh.parsers import format_privmsg, parse_syslog

BAD = re.compile(r"[\r\n\x00]")
DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"
NAMES = ["help", "status", "incidents", "explain", "resolve", "proposals", "approve", "deny", "foo"]


@given(st.text())
def test_parse_command_never_raises_and_names_are_clean(text):
    cmd = parse_command(text)
    if cmd is None:
        return
    assert text.startswith("!")
    assert cmd.name and cmd.name == cmd.name.lower()
    assert not any(ch.isspace() for ch in cmd.name)
    assert len(cmd.args) <= 5
    assert all(a and not any(ch.isspace() for ch in a) for a in cmd.args)


@given(st.text().map(lambda s: "!" + s))
def test_bang_text_parses_or_returns_none(text):
    assert parse_command(text) is None or isinstance(parse_command(text), Command)


def _hub():
    hub = Hub()
    hub.operators = None
    hub.sent = []

    async def fake(channel, text):
        hub.sent.append((channel, text))

    hub.irc_broadcast = fake
    return hub


arg = st.one_of(st.text(max_size=40), st.sampled_from(["p1", "p2", "P3", "nope"]))


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(NAMES), st.lists(arg, max_size=6), st.text(max_size=20)), max_size=15))
def test_replies_never_contain_crlf_nul(calls):
    async def run():
        hub = _hub()
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        iid = hub.incidents()[0].id
        await hub.create_proposal(iid, "a\r\nb")
        for name, args, nick in calls:
            args = tuple(args[:5])
            replies = await handle_command(hub, nick, "#ops", Command(name, args))
            assert all(not BAD.search(r) for r in replies)
        for channel, text in hub.sent:
            assert all(line.count("\r") == 0 and "\n" not in line and "\x00" not in line
                       for line in format_privmsg(channel, text))
    asyncio.run(run())


op = st.tuples(st.sampled_from(["create", "approve", "deny", "complete"]), st.integers(0, 6))


@settings(max_examples=60, deadline=None)
@given(st.lists(op, max_size=40))
def test_proposal_state_machine_stays_valid(ops):
    async def run():
        hub = _hub()
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        iid = hub.incidents()[0].id
        for kind, n in ops:
            pid = f"p{n}"
            before = hub.get_proposal(pid).status if hub.get_proposal(pid) else None
            if kind == "create":
                await hub.create_proposal(iid, "x")
                continue
            if kind == "complete":
                result = await hub.complete(pid, "n")
                allowed = before == "approved"
            else:
                result = await hub.decide(pid, kind == "approve", "t")
                allowed = before == "pending"
            assert (result is not None) == allowed
        for p in hub.list_proposals():
            assert p.status in PROPOSAL_STATUSES
            assert (p.decided_by != "") == (p.status != "pending")
    asyncio.run(run())
