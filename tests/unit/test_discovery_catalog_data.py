"""The shipped catalog and its packaging (R3.1, R3.3, R3.4, Success Criterion 5)."""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from chimericmesh.discovery.catalog import CORE_APP_IDS, KINDS, load_catalog, validate_catalog
from chimericmesh.discovery.expand import ALLOWED_ENV
from chimericmesh.parsers.formats import FORMAT_NAMES
from chimericmesh.services.validate import DEFAULT_FORMATS

ROOT = Path(__file__).resolve().parents[2]
KNOWN = frozenset(FORMAT_NAMES)
PACKAGED = "catalog/legacy_apps.json"


def _text() -> str:
    return resources.files("chimericmesh").joinpath("catalog", "legacy_apps.json").read_text("utf-8")


def test_packaged_file_loads_and_validates():
    data = json.loads(_text())
    assert validate_catalog(data, KNOWN) == []
    load = load_catalog(KNOWN)
    assert load.catalog is not None and load.reason == ""


def test_at_least_14_entries_and_exactly_the_15_core_ids():
    cat = load_catalog(KNOWN).catalog
    ids = {e.app_id for e in cat.entries}
    assert len(cat.entries) >= 14
    assert ids == set(CORE_APP_IDS) and len(ids) == 15


def test_every_format_is_known_to_the_registry_and_lists_cannot_drift():
    assert DEFAULT_FORMATS == frozenset(FORMAT_NAMES)
    for entry in load_catalog(KNOWN).catalog.entries:
        for rules in entry.log_paths.values():
            for rule in rules:
                assert rule.format in KNOWN and rule.kind in KINDS


def test_no_mosquitto_or_mqtt():
    assert "mosquitto" not in _text().lower() and "mqtt" not in _text().lower()


def test_placeholders_are_allow_listed_and_only_at_the_start():
    for entry in load_catalog(KNOWN).catalog.entries:
        for os_name, rules in entry.log_paths.items():
            for rule in rules:
                if os_name == "windows" and "%" in rule.path:
                    assert rule.path.startswith("%") and rule.path.count("%") == 2
                    name = rule.path.split("%")[1]
                    assert name in ALLOWED_ENV["windows"]
                assert "$" not in rule.path or rule.path.startswith("$HOME/")


def test_no_rule_matches_rotated_or_compressed_siblings():
    for entry in load_catalog(KNOWN).catalog.entries:
        for rules in entry.log_paths.values():
            for rule in rules:
                last = rule.path.replace("\\", "/").rsplit("/", 1)[-1]
                assert not last.endswith("*") and ".gz" not in last


def test_legacy_app_demo_is_not_in_the_catalog():
    assert "legacy" not in _text().lower().replace("legacy_apps.json", "")


def test_pyproject_ships_the_catalog():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = cfg["tool"]["setuptools"]["package-data"]["chimericmesh"]
    assert "web/*.html" in patterns and "catalog/*.json" in patterns
    assert any(fnmatch.fnmatch(PACKAGED, p) for p in patterns)
    assert (ROOT / "src" / "chimericmesh" / "catalog" / "legacy_apps.json").exists()
    assert not (ROOT / "src" / "chimericmesh" / "catalog" / "__init__.py").exists()


def test_contingency_edit_still_validates():
    text = _text().replace('"w3c"', '"plain"').replace('"xferlog"', '"plain"')
    assert load_catalog(frozenset({"syslog-text", "ncsa", "plain"}), text=text).catalog is not None


def test_non_ascii_free_and_utf8():
    raw = (ROOT / "src" / "chimericmesh" / "catalog" / "legacy_apps.json").read_bytes()
    assert raw.decode("utf-8").isascii()


def _can_build() -> bool:
    if os.environ.get("CHIMERICMESH_SKIP_BUILD") == "1":
        return False
    try:
        import pip  # noqa: F401
        import setuptools  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _can_build(), reason="CHIMERICMESH_SKIP_BUILD=1 or pip/setuptools unavailable")
def test_wheel_contains_the_catalog(tmp_path):
    src = tmp_path / "src"
    import shutil
    shutil.copytree(ROOT / "src", src, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    out = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(out)],
        cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        assert "chimericmesh/catalog/legacy_apps.json" in zf.namelist()
