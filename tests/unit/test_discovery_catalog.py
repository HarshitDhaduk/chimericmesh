"""Catalog validator and loader (R3.1, R3.2, R3.4, R3.5)."""
from __future__ import annotations

import copy
import json
import logging

import pytest

from chimericmesh.discovery.catalog import (
    CORE_APP_IDS,
    Catalog,
    CatalogLoad,
    fingerprint,
    load_catalog,
    validate_catalog,
)

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})


def app(**over):
    base = {
        "app_id": "nginx", "name": "nginx", "process_names": ["nginx"], "ports": [80],
        "protocol_label": "http", "justification": None,
        "log_paths": {
            "linux": [{"path": "/var/log/nginx/access*.log", "format": "ncsa", "kind": "access"}],
            "windows": [{"path": "%SystemDrive%\\nginx\\logs\\access.log", "format": "ncsa", "kind": "access"}],
            "macos": [],
        },
    }
    base.update(over)
    return base


def doc(*apps):
    return {"version": 1, "apps": list(apps) or [app()]}


def errors(data):
    return validate_catalog(data, FORMATS)


def with_rule(**over):
    a = app()
    a["log_paths"]["linux"][0].update(over)
    return doc(a)


def test_minimal_document_is_valid_and_loads():
    assert errors(doc()) == []
    load = load_catalog(FORMATS, text=json.dumps(doc()))
    assert isinstance(load, CatalogLoad) and load.reason == "" and isinstance(load.catalog, Catalog)
    assert load.catalog.entries[0].app_id == "nginx" and load.catalog.version == 1


@pytest.mark.parametrize("data,needle", [
    ("x", "object"),
    ({"version": 2, "apps": [app()]}, "version"),
    ({"version": True, "apps": [app()]}, "version"),
    ({"version": 1, "apps": []}, "apps"),
    ({"version": 1, "apps": "no"}, "apps"),
    ({"version": 1, "apps": [app()], "extra": 1}, "extra"),
    (doc(app(extra=1)), "extra"),
    (doc(app(app_id="Nginx")), "app_id"),
    (doc(app(app_id="")), "app_id"),
    (doc(app(app_id="x" * 41)), "app_id"),
    (doc(app(), app()), "duplicate"),
    (doc(app(name="")), "name"),
    (doc(app(name="n" * 81)), "name"),
    (doc(app(process_names=[])), "process_names"),
    (doc(app(process_names=["nginx.exe"])), "process_names"),
    (doc(app(process_names=["NGINX"])), "process_names"),
    (doc(app(process_names=["a/b"])), "process_names"),
    (doc(app(process_names=["a\x00"])), "process_names"),
    (doc(app(process_names=["x"] * 11)), "process_names"),
    (doc(app(ports=[0])), "ports"),
    (doc(app(ports=[65536])), "ports"),
    (doc(app(ports=[True])), "ports"),
    (doc(app(ports=list(range(1, 22)))), "ports"),
    (doc(app(protocol_label="")), "protocol_label"),
    (doc(app(protocol_label="HTTP!")), "protocol_label"),
    (doc(app(justification="")), "justification"),
    (doc(app(justification="j" * 201)), "justification"),
])
def test_document_level_rules(data, needle):
    found = errors(data)
    assert found and any(needle in e for e in found), found


def test_every_os_key_must_be_present_and_one_non_empty():
    a = app()
    del a["log_paths"]["macos"]
    assert any("macos" in e for e in errors(doc(a)))
    a = app(log_paths={"linux": [], "windows": [], "macos": []})
    assert any("log_paths" in e for e in errors(doc(a)))
    a = app()
    a["log_paths"]["solaris"] = []
    assert any("solaris" in e for e in errors(doc(a)))
    a = app()
    a["log_paths"]["linux"] = [a["log_paths"]["linux"][0]] * 21
    assert any("20" in e or "linux" in e for e in errors(doc(a)))


@pytest.mark.parametrize("path", [
    "relative/x.log",
    "/var/../etc/x.log",
    "/var/**/x.log",
    "/var/[a]/x.log",
    "/a*/b*/c*/x.log",
    "/var/log/x\x00.log",
    "/var/log/" + "x" * 520,
    "/var/log/x/$HOME/y.log",
    "$SECRET/x.log",
    "/var/log/nginx/access*",
    "/var/log/nginx/*",
])
def test_linux_path_rules(path):
    found = errors(with_rule(path=path))
    assert found and any("path" in e for e in found), (path, found)


@pytest.mark.parametrize("path", [
    "inetpub\\x.log",
    "C:\\a\\%SystemDrive%\\x.log",
    "%PATH%\\x.log",
    "%Nope%\\x.log",
    "C:\\a*\\b*\\c*\\x.log",
    "C:\\a\\..\\x.log",
    "C:\\a\\x*",
])
def test_windows_path_rules(path):
    a = app()
    a["log_paths"]["windows"][0]["path"] = path
    found = errors(doc(a))
    assert found and any("windows" in e and "path" in e for e in found), (path, found)


def test_valid_placeholder_and_wildcard_paths_pass():
    a = app()
    a["log_paths"]["windows"] = [
        {"path": "%SystemDrive%\\inetpub\\logs\\LogFiles\\W3SVC*\\*.log", "format": "w3c", "kind": "access"},
        {"path": "%ProgramFiles(x86)%\\FileZilla Server\\Logs\\*.log", "format": "plain", "kind": "ftp"},
        {"path": "C:\\Apache24\\logs\\access.log", "format": "ncsa", "kind": "access"},
    ]
    a["log_paths"]["linux"] = [{"path": "$HOME/logs/*.log", "format": "plain", "kind": "other"}]
    assert errors(doc(a)) == []


def test_format_and_kind_rules():
    assert any("format" in e for e in errors(with_rule(format="nope")))
    assert any("kind" in e for e in errors(with_rule(kind="weird")))
    assert any("extra" in e for e in errors(with_rule(extra=1)))
    a = app()
    del a["log_paths"]["linux"][0]["format"]
    assert any("format" in e for e in errors(doc(a)))


def test_non_core_app_needs_a_justification_core_does_not():
    assert "nginx" in CORE_APP_IDS and len(CORE_APP_IDS) == 15
    mosq = app(app_id="mosquitto", name="Mosquitto", process_names=["mosquitto"], ports=[1883])
    assert any("justification" in e for e in errors(doc(mosq)))
    mosq["justification"] = "legacy broker config shipped on old appliances"
    assert errors(doc(mosq)) == []


def test_load_catalog_never_raises_and_logs_one_error(caplog):
    bad = json.dumps({"version": 9, "apps": []})
    with caplog.at_level(logging.ERROR, logger="chimericmesh.discovery"):
        res = load_catalog(FORMATS, text=bad)
    assert res.catalog is None and res.reason
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    for text in ("not json", "", "[]", "null", "[" * 100000):
        res = load_catalog(FORMATS, text=text)
        assert res.catalog is None and res.reason


def test_validate_is_total_on_odd_values():
    for value in (None, 1, "s", [], [[]], {"version": 1, "apps": [None]}, {"version": 1, "apps": [{}]},
                  {"version": 1, "apps": [app(log_paths=5)]}, {"version": 1, "apps": [app(ports="x")]}):
        assert isinstance(errors(value), list)


def test_process_index_is_keyed_by_15_characters_on_linux_only():
    a = app(process_names=["filezilla-server", "nginx"])
    cat = load_catalog(FORMATS, text=json.dumps(doc(a))).catalog
    assert cat is not None
    linux = cat.process_index("linux")
    assert "filezilla-serve" in linux and "filezilla-server" not in linux and "nginx" in linux
    win = cat.process_index("windows")
    assert "filezilla-server" in win and "filezilla-serve" not in win
    assert cat.process_index("linux") is linux          # cached


def test_fingerprint_is_stable_and_sensitive():
    cat = load_catalog(FORMATS, text=json.dumps(doc())).catalog
    entry = cat.entries[0]
    fp = fingerprint(entry, "linux")
    assert len(fp) == 16 and int(fp, 16) >= 0
    assert fp == fingerprint(entry, "linux")
    assert fp != fingerprint(entry, "windows")
    a = app()
    a["log_paths"]["linux"].append({"path": "/var/log/nginx/error*.log", "format": "plain", "kind": "error"})
    swapped = copy.deepcopy(a)
    swapped["log_paths"]["linux"].reverse()
    e1 = load_catalog(FORMATS, text=json.dumps(doc(a))).catalog.entries[0]
    e2 = load_catalog(FORMATS, text=json.dumps(doc(swapped))).catalog.entries[0]
    assert fingerprint(e1, "linux") == fingerprint(e2, "linux") != fp
