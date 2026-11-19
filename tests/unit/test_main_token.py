"""Entry point: ``token --rotate``, the token path line and the exit codes (R6.1, R6.2, R6.8)."""
from __future__ import annotations

import pytest

import chimericmesh.__main__ as entry
from chimericmesh.api import create_app


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "cm-home"
    monkeypatch.setenv("CHIMERICMESH_HOME", str(h))
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    monkeypatch.delenv("CHIMERICMESH_TOKEN_FILE", raising=False)
    return h


@pytest.fixture
def no_server(monkeypatch):
    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: (calls.append(app), app.state.store.close()))
    return calls


def test_rotate_replaces_the_file_and_prints_only_its_path(home, capsys):
    path = home / "admin.token"
    with pytest.raises(SystemExit) as first:
        entry.main(["token", "--rotate"])
    assert first.value.code == 0
    old = path.read_text().strip()
    capsys.readouterr()
    with pytest.raises(SystemExit) as info:
        entry.main(["token", "--rotate"])
    assert info.value.code == 0
    new = path.read_text().strip()
    out = capsys.readouterr()
    assert new != old and len(new) >= 43
    assert out.out.strip() == str(path)
    assert new not in out.out + out.err and old not in out.out + out.err


def test_token_without_rotate_prints_usage_and_exits_2(home, capsys):
    with pytest.raises(SystemExit) as info:
        entry.main(["token"])
    assert info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_normal_start_prints_the_token_path_never_the_token(home, no_server, capsys):
    entry.main([])
    assert no_server
    out = capsys.readouterr()
    path = home / "admin.token"
    token = path.read_text().strip()
    assert f"admin token file: {path}" in out.out
    assert token not in out.out + out.err


def test_memory_database_prints_no_path(home, no_server, monkeypatch, capsys):
    monkeypatch.setenv("CHIMERICMESH_DB", ":memory:")
    entry.main([])
    out = capsys.readouterr()
    assert "admin token file" not in out.out
    assert not (home / "admin.token").exists()


def test_unusable_token_file_exits_2_with_the_message(home, no_server, capsys):
    home.mkdir(parents=True)
    (home / "admin.token").write_text("not a token\n")
    with pytest.raises(SystemExit) as info:
        entry.main([])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "token --rotate" in err and str(home / "admin.token") in err
    assert not no_server


def test_rotation_reaches_a_running_app_only_after_restart(home):
    running = create_app(start_listeners=False)
    try:
        before = running.state.access.tokens.current()
        with pytest.raises(SystemExit):
            entry.main(["token", "--rotate"])
        assert running.state.access.tokens.current() == before
    finally:
        running.state.store.close()
    fresh = create_app(start_listeners=False)
    try:
        assert fresh.state.access.tokens.current() == (home / "admin.token").read_text().strip() != before
    finally:
        fresh.state.store.close()
