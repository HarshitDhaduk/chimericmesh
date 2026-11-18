import logging
import os
from pathlib import Path

import pytest

from chimericmesh.store.config import StoreConfig, resolve_db_path, resolve_home


def test_default_path_is_under_home_and_absolute(tmp_path):
    env = {"CHIMERICMESH_HOME": str(tmp_path / "h")}
    path = resolve_db_path(env)
    assert os.path.isabs(path)
    assert Path(path) == tmp_path / "h" / "chimericmesh.db"


def test_default_home_is_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = resolve_home({})
    assert os.path.isabs(home)
    assert Path(home) == tmp_path / ".chimericmesh"


def test_home_override_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert Path(resolve_home({"CHIMERICMESH_HOME": "~/x"})) == tmp_path / "x"


def test_db_override_and_memory(tmp_path):
    target = tmp_path / "elsewhere.db"
    assert Path(resolve_db_path({"CHIMERICMESH_DB": str(target)})) == target
    assert resolve_db_path({"CHIMERICMESH_DB": ":memory:"}) == ":memory:"


def test_relative_db_resolved_once_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = resolve_db_path({"CHIMERICMESH_DB": "rel.db"})
    assert os.path.isabs(path)
    assert Path(path) == tmp_path / "rel.db"


def test_two_working_directories_open_the_same_default_file(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    env = dict(os.environ)
    monkeypatch.chdir(a)
    first = resolve_db_path(env)
    monkeypatch.chdir(b)
    second = resolve_db_path(env)
    assert first == second
    assert not Path(first).is_relative_to(a) and not Path(first).is_relative_to(b)


def test_resolution_touches_no_files(tmp_path):
    home = tmp_path / "never-created"
    resolve_db_path({"CHIMERICMESH_HOME": str(home)})
    assert not home.exists()


def test_defaults():
    cfg = StoreConfig.from_env({})
    assert (cfg.max_events, cfg.event_ttl_days, cfg.reopen_after) == (100000, 7, 300)
    assert (cfg.batch_rows, cfg.batch_ms, cfg.queue_limit) == (500, 250, 20000)
    assert (cfg.trim_interval, cfg.flush_interval, cfg.flush_tick, cfg.persist_slots) == (60.0, 30.0, 5.0, 256)


@pytest.mark.parametrize(
    "name,field,default",
    [
        ("CHIMERICMESH_MAX_EVENTS", "max_events", 100000),
        ("CHIMERICMESH_EVENT_TTL_DAYS", "event_ttl_days", 7),
        ("CHIMERICMESH_REOPEN_AFTER_SECONDS", "reopen_after", 300),
    ],
)
@pytest.mark.parametrize("bad", ["abc", "", "1.5", "-1", "99999999999", " "])
def test_invalid_values_warn_and_fall_back(name, field, default, bad, caplog):
    with caplog.at_level(logging.WARNING):
        cfg = StoreConfig.from_env({name: bad})
    assert getattr(cfg, field) == default
    assert any(name in r.getMessage() for r in caplog.records)


def test_reopen_range_edges(caplog):
    assert StoreConfig.from_env({"CHIMERICMESH_REOPEN_AFTER_SECONDS": "0"}).reopen_after == 0
    assert StoreConfig.from_env({"CHIMERICMESH_REOPEN_AFTER_SECONDS": "86400"}).reopen_after == 86400
    with caplog.at_level(logging.WARNING):
        cfg = StoreConfig.from_env({"CHIMERICMESH_REOPEN_AFTER_SECONDS": "86401"})
    assert cfg.reopen_after == 300 and caplog.records


def test_other_ranges_accept_values():
    cfg = StoreConfig.from_env(
        {"CHIMERICMESH_MAX_EVENTS": "1000", "CHIMERICMESH_EVENT_TTL_DAYS": "3650"}
    )
    assert (cfg.max_events, cfg.event_ttl_days) == (1000, 3650)
    assert StoreConfig.from_env({"CHIMERICMESH_MAX_EVENTS": "0"}).max_events == 100000
    assert StoreConfig.from_env({"CHIMERICMESH_EVENT_TTL_DAYS": "3651"}).event_ttl_days == 7


def test_timing_knobs_overridable():
    cfg = StoreConfig.from_env({}, batch_ms=10, queue_limit=100)
    assert (cfg.batch_ms, cfg.queue_limit) == (10, 100)
