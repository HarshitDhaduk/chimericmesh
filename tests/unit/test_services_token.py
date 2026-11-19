"""Token store (R6.1, R6.2, R6.8)."""
from __future__ import annotations
import logging
import os
import re
import stat

import pytest

from chimericmesh.services import token as token_mod
from chimericmesh.services.errors import AccessConfigError
from chimericmesh.services.token import TokenStore, resolve_token_path

URLSAFE = re.compile(r"[A-Za-z0-9_-]{43,}")


@pytest.fixture(autouse=True)
def fake_perms(monkeypatch):
    """Record fsperms calls instead of running icacls."""
    calls = []
    monkeypatch.setattr(token_mod.fsperms, "secure_file", lambda p, **kw: calls.append(("file", str(p))) or True)
    monkeypatch.setattr(token_mod.fsperms, "secure_dir", lambda p, **kw: calls.append(("dir", str(p))) or True)
    return calls


def test_new_file_holds_urlsafe_token(tmp_path, fake_perms):
    path = tmp_path / "home" / "admin.token"
    ts = TokenStore.open(path, in_memory=False)
    text = path.read_text().strip()
    assert URLSAFE.fullmatch(text) and ts.current() == text
    assert ("file", str(path)) in fake_perms and ("dir", str(path.parent)) in fake_perms
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_default_path_and_override(tmp_path):
    assert resolve_token_path({}, tmp_path) == tmp_path / "admin.token"
    assert resolve_token_path({"CHIMERICMESH_TOKEN_FILE": str(tmp_path / "t.txt")}, tmp_path) == tmp_path / "t.txt"


def test_two_working_directories_give_the_same_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    monkeypatch.chdir(a)
    p1 = resolve_token_path({}, home)
    t1 = TokenStore.open(p1, in_memory=False).current()
    monkeypatch.chdir(b)
    p2 = resolve_token_path({}, home)
    assert p1 == p2 and TokenStore.open(p2, in_memory=False).current() == t1


def test_path_under_working_directory_logs_one_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.services"):
        TokenStore.open(tmp_path / "sub" / "t", in_memory=False)
    assert len([r for r in caplog.records if "working directory" in r.getMessage()]) == 1


def test_no_warning_outside_working_directory(tmp_path, monkeypatch, caplog):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.services"):
        TokenStore.open(tmp_path / "other" / "t", in_memory=False)
    assert not [r for r in caplog.records if "working directory" in r.getMessage()]


def test_zero_byte_file_is_regenerated(tmp_path):
    path = tmp_path / "t"
    path.write_bytes(b"")
    assert URLSAFE.fullmatch(TokenStore.open(path, in_memory=False).current())


def test_invalid_file_raises_naming_path_and_rotate(tmp_path):
    path = tmp_path / "t"
    path.write_text("short")
    with pytest.raises(AccessConfigError) as err:
        TokenStore.open(path, in_memory=False)
    assert str(path) in str(err.value) and "token --rotate" in str(err.value)
    assert path.read_text() == "short"


def test_existing_valid_file_is_read(tmp_path):
    path = tmp_path / "t"
    path.write_text("A" * 43 + "\n")
    assert TokenStore.open(path, in_memory=False).current() == "A" * 43


def test_o_excl_loss_reads_the_winner(tmp_path, monkeypatch):
    path = tmp_path / "t"
    real_open = os.open
    winner = "W" * 43

    def racing_open(p, flags, mode=0o777):
        if str(p) == str(path) and flags & os.O_EXCL:
            path.write_text(winner + "\n")       # another process won the race
            raise FileExistsError(p)
        return real_open(p, flags, mode)

    monkeypatch.setattr(token_mod.os, "open", racing_open)
    assert TokenStore.open(path, in_memory=False).current() == winner


def test_in_memory_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ts = TokenStore.open(None, in_memory=True)
    assert ts.path is None and URLSAFE.fullmatch(ts.current())
    assert list(tmp_path.iterdir()) == []


def test_current_never_rereads_the_file(tmp_path):
    path = tmp_path / "t"
    ts = TokenStore.open(path, in_memory=False)
    first = ts.current()
    path.write_text("B" * 50 + "\n")
    assert ts.current() == first


def test_rotate_replaces_atomically_and_old_store_keeps_old_token(tmp_path):
    path = tmp_path / "t"
    old = TokenStore.open(path, in_memory=False)
    before = old.current()
    old.rotate()
    after = path.read_text().strip()
    assert after != before and URLSAFE.fullmatch(after)
    assert old.current() == before
    assert TokenStore.open(path, in_memory=False).current() == after
    assert [p.name for p in tmp_path.iterdir()] == ["t"]


def test_rotate_failure_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "t"
    ts = TokenStore.open(path, in_memory=False)
    before = path.read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(token_mod.os, "replace", boom)
    with pytest.raises(OSError):
        ts.rotate()
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["t"]


def test_repr_contains_no_token(tmp_path):
    from chimericmesh.services.access import AccessControl
    ts = TokenStore.open(tmp_path / "t", in_memory=False)
    ac = AccessControl(enforce=True, tokens=ts, allowed_hosts=frozenset({"localhost"}), audit=None)
    assert ts.current() not in repr(ts) and ts.current() not in repr(ac) and ts.current() not in str(ac)


def test_add_redaction_called_with_the_token(tmp_path):
    seen = []
    ts = TokenStore.open(tmp_path / "t", in_memory=False, redact=seen.append)
    assert seen == [ts.current()]
    seen2 = []
    ts2 = TokenStore.open(None, in_memory=True, redact=seen2.append)
    assert seen2 == [ts2.current()]
