"""scripts/irc_client.py --password / --token-file against an enforcing in-process IrcServer."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from chimericmesh.hub import Hub
from chimericmesh.listeners import IrcServer
from chimericmesh.parsers import parse_syslog
from chimericmesh.services.access import AccessControl
from chimericmesh.services.token import TokenStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "irc_client.py"
DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


async def setup():
    hub = Hub(enforce_access=True)
    hub.operators = {"op1"}
    ts = TokenStore.open(None, in_memory=True)
    hub.access = AccessControl(enforce=True, tokens=ts, allowed_hosts=frozenset({"localhost"}), audit=None)
    irc = IrcServer(hub)
    server = await irc.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for i in range(3):
        await hub.ingest(parse_syslog(DISK % i))
    prop = await hub.create_proposal(hub.incidents()[0].id, "rotate logs")
    return hub, prop, server, port, ts.current()


def client(port, stdin, *extra, env=None):
    return asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(SCRIPT), "--port", str(port), "--nick", "op1", "--linger", "1", *extra],
        input=stdin, capture_output=True, text=True, timeout=30, env=env)


def test_right_password_lets_operator_approve_and_secret_is_not_printed():
    async def go():
        hub, prop, server, port, token = await setup()
        try:
            r = await client(port, f"!approve {prop.id}\n", "--password", token)
            assert prop.status == "approved"
            assert token not in r.stdout and token not in r.stderr
        finally:
            server.close()
    asyncio.run(go())


def test_token_file_works_and_secret_is_not_printed(tmp_path):
    async def go():
        hub, prop, server, port, token = await setup()
        f = tmp_path / "admin.token"
        f.write_text(token + "\n")
        try:
            r = await client(port, f"!approve {prop.id}\n", "--token-file", str(f))
            assert prop.status == "approved"
            assert token not in r.stdout and token not in r.stderr
        finally:
            server.close()
    asyncio.run(go())


def test_default_token_file_honours_env(tmp_path):
    import os

    async def go():
        hub, prop, server, port, token = await setup()
        f = tmp_path / "custom.token"
        f.write_text(token + "\n")
        env = {**os.environ, "CHIMERICMESH_TOKEN_FILE": str(f)}
        try:
            await client(port, f"!approve {prop.id}\n", "--token-file", env=env)
            assert prop.status == "approved"
        finally:
            server.close()
    asyncio.run(go())


def test_wrong_password_gets_464_and_cannot_approve():
    async def go():
        hub, prop, server, port, token = await setup()
        try:
            r = await client(port, f"!approve {prop.id}\n", "--password", "wrong-secret-value")
            assert "464" in r.stdout
            assert prop.status == "pending"
            assert "wrong-secret-value" not in r.stdout
        finally:
            server.close()
    asyncio.run(go())


def test_control_characters_in_password_refuse_to_start():
    for bad in ("abc\r\nQUIT", "abc\ndef"):
        r = subprocess.run([sys.executable, str(SCRIPT), "--port", "1", "--password", bad],
                           input="", capture_output=True, text=True, timeout=30)
        assert r.returncode == 2
        assert "abc" not in r.stdout + r.stderr


def test_control_characters_in_token_file_refuse_to_start(tmp_path):
    f = tmp_path / "t"
    f.write_bytes(b"abc\x00def\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "--port", "1", "--token-file", str(f)],
                       input="", capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "abc" not in r.stdout + r.stderr
