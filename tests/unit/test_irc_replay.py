"""IRC replay, nickname masking, chat severity cap, and sender-spoofing protection."""

import asyncio
import re

import pytest

from chimericmesh.hub import Hub
from chimericmesh.listeners import IrcServer
from chimericmesh.parsers import irc_to_event
from chimericmesh.replay_irc import (IRC_DATASETS, Masker, load_chat, parse_ubuntu_line,
                                     replay_chat, safe_nick)

LOG = """\
[00:10] <andre_pl> this live disk doesn't have smartctl
=== sarnold joined #ubuntu-server
[00:11] <sarnold> andre_pl: try lshw?
* ebonics waves
[00:12] <andre_pl> there we go, thanks :)
[00:13] <zerowaitstate> sarnold, it often does
"""


def test_parse_ubuntu_line():
    assert parse_ubuntu_line("[00:11] <sarnold> try lshw?") == ("sarnold", "try lshw?")
    assert parse_ubuntu_line("=== sarnold joined #x") is None
    assert parse_ubuntu_line("* ebonics waves") is None
    assert parse_ubuntu_line("[00:11] <sarnold>   ") is None


def test_load_chat_filters_and_limits(tmp_path):
    f = tmp_path / "c.log"
    f.write_text(LOG)
    assert [n for n, _ in load_chat(f)] == ["andre_pl", "sarnold", "andre_pl", "zerowaitstate"]
    assert len(load_chat(f, limit=2)) == 2


def test_masker_hides_nicks_everywhere_and_is_stable():
    chat = [("andre_pl", "hi"), ("sarnold", "andre_pl: try lshw?"), ("zerowaitstate", "sarnold, it often does")]
    m = Masker(n for n, _ in chat)
    assert len(set(m.map.values())) == 3
    out = " ".join(m.text(t) for _, t in chat)
    assert "sarnold" not in out and "andre_pl" not in out
    assert m.map["sarnold"] in out and m.map["andre_pl"] in out
    assert Masker(n for n, _ in chat).map == m.map  # deterministic


def test_masker_does_not_mangle_ordinary_words():
    m = Masker(["bob", "mike"])
    # Short nicks (< 4 chars) are not replaced mid-text; longer ones only as whole words.
    assert m.text("bobsled") == "bobsled"
    assert m.text("the mike is on") == "the " + m.map["mike"] + " is on"


def test_pseudonyms_have_no_digits_or_dashes_even_past_the_name_list():
    m = Masker(f"user{i}" for i in range(100))
    assert all(re.fullmatch(r"[A-Za-z]+", v) for v in m.map.values())
    assert len(set(m.map.values())) == 100


def test_safe_nick_strips_dangerous_characters():
    assert safe_nick("bob\r\nQUIT") == "bobQUIT"
    assert safe_nick("a b") == "ab"
    assert safe_nick("\r\n") == "anon"
    assert len(safe_nick("x" * 100)) == 30


def test_dataset_allowlist_is_https_ubuntu_only():
    assert all(u.startswith("https://irclogs.ubuntu.com/") for u, _ in IRC_DATASETS.values())


def test_chat_cannot_open_an_incident_alone():
    ev = irc_to_event(":x!u@h PRIVMSG #ops :kernel panic and the OOM killer killed everything")
    assert ev.severity == 3  # capped at err; syslog would call this crit


def test_irc_alert_lines_still_open_after_three_repeats():
    from chimericmesh.diagnostics import analyze

    evs = [irc_to_event(":x!u@h PRIVMSG #ops :web02: upstream timed out connecting to 10.0.4.17:5432")] * 3
    assert len(analyze(evs)) == 1


def test_invalid_channel_rejected():
    with pytest.raises(ValueError):
        replay_chat([("a", "b")], ("127.0.0.1", 1), "#bad channel\r\nQUIT")


def _serve():
    hub = Hub()
    return hub, IrcServer(hub)


def test_forged_prefix_is_ignored_and_unregistered_clients_cannot_speak():
    async def run():
        hub, irc = _serve()
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b":admin!root@trusted PRIVMSG #ops :nobody registered me\r\n")  # not registered yet
            w.write(b"NICK mallory\r\nUSER m 0 * :m\r\nJOIN #ops\r\n")
            w.write(b":admin!root@trusted PRIVMSG #ops :forged sender\r\n")
            await w.drain()
            await asyncio.sleep(0.3)
            hosts = [e.host for e in hub.events]
            assert hosts == ["mallory"], hosts  # forged prefix replaced, unregistered line dropped
            w.close()
        finally:
            server.close()

    asyncio.run(run())


def test_replay_chat_end_to_end_with_masking(tmp_path):
    f = tmp_path / "c.log"
    f.write_text(LOG)

    async def run():
        hub, irc = _serve()
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            stats = await asyncio.to_thread(replay_chat, load_chat(f), ("127.0.0.1", port), "#ubuntu-server", 0.0)
            await asyncio.sleep(0.3)
            assert stats == {"sent": 4, "skipped": 0, "speakers": 3}
            hosts = {e.host for e in hub.events}
            assert len(hosts) == 3 and not hosts & {"andre_pl", "sarnold", "zerowaitstate"}
            assert all("sarnold" not in e.message and "andre_pl" not in e.message for e in hub.events)
        finally:
            server.close()

    asyncio.run(run())


def test_max_nicks_skips_extra_speakers():
    chat = [(f"user{i}", f"hello {i}") for i in range(5)]

    async def run():
        hub, irc = _serve()
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            stats = await asyncio.to_thread(replay_chat, chat, ("127.0.0.1", port), "#ops", 0.0, True, 3)
            assert stats["speakers"] == 3 and stats["skipped"] == 2 and stats["sent"] == 3
        finally:
            server.close()

    asyncio.run(run())
