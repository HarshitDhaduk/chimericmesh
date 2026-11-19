"""Field validators of the services module (R1.2 to R1.4, R3.1, R8.3)."""
from __future__ import annotations

import os

import pytest

from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.models import CLEAR, LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from chimericmesh.services.validate import (
    DEFAULT_FORMATS,
    KNOWN_PROTOCOLS,
    clean_text,
    validate_log_source_fields,
    validate_service_fields,
    validate_slug,
)


@pytest.mark.parametrize("value", ["a", "proxy-worker", "a" * 40, "0", "9-lives"])
def test_slug_accepts(value):
    assert validate_slug(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "a" * 41, "Upper", "-lead", "has space", "abc\n", "a\x00b", "unassigned", "a_b", "é"],
)
def test_slug_rejects(value):
    with pytest.raises(ValidationFailed) as exc:
        validate_slug(value)
    assert exc.value.field == "id"


@pytest.mark.parametrize("value", [None, 5, b"abc", ["a"]])
def test_slug_rejects_non_text(value):
    with pytest.raises(ValidationFailed):
        validate_slug(value)


def test_clean_text_removes_controls_and_surrogates():
    assert clean_text("  a\x00b\x1fc\x7fd\x85e\ud800f  ") == "abcdef"
    assert clean_text("tab\there\r\n") == "tabhere"


def _create(**over):
    raw = {"id": "web", "name": "Web"}
    raw.update(over)
    return validate_service_fields(raw, for_create=True)


def test_create_defaults():
    got = _create()
    assert got == ServiceFields(id="web", name="Web")


def test_name_cap_and_field():
    assert _create(name="n" * 80).name == "n" * 80
    with pytest.raises(ValidationFailed) as exc:
        _create(name="n" * 81)
    assert exc.value.field == "name"


def test_name_required_and_nonempty_after_cleaning():
    with pytest.raises(ValidationFailed) as exc:
        validate_service_fields({"id": "web"}, for_create=True)
    assert exc.value.field == "name"
    with pytest.raises(ValidationFailed) as exc:
        _create(name=" \x00 ")
    assert exc.value.field == "name"


def test_id_required_on_create():
    with pytest.raises(ValidationFailed) as exc:
        validate_service_fields({"name": "x"}, for_create=True)
    assert exc.value.field == "id"


@pytest.mark.parametrize("field", ["hosts", "app_names"])
def test_list_caps(field):
    assert len(getattr(_create(**{field: [f"h{i}" for i in range(20)]}), field)) == 20
    with pytest.raises(ValidationFailed) as exc:
        _create(**{field: [f"h{i}" for i in range(21)]})
    assert exc.value.field == field
    with pytest.raises(ValidationFailed) as exc:
        _create(**{field: ["x" * 101]})
    assert exc.value.field == field
    assert getattr(_create(**{field: ["x" * 100]}), field) == ("x" * 100,)


@pytest.mark.parametrize("field", ["hosts", "app_names"])
def test_dedupe_is_case_insensitive_and_keeps_first(field):
    got = getattr(_create(**{field: ["Alpha", "beta", "ALPHA", "Beta", "gamma"]}), field)
    assert got == ("Alpha", "beta", "gamma")


def test_cap_applies_after_dedupe():
    entries = [f"h{i}" for i in range(20)] + ["H0", "H1"]
    assert len(_create(hosts=entries).hosts) == 20


def test_empty_entry_after_cleaning_is_an_error():
    with pytest.raises(ValidationFailed) as exc:
        _create(hosts=["ok", "\x00 "])
    assert exc.value.field == "hosts"


def test_entries_are_cleaned():
    assert _create(hosts=[" a\x00b "]).hosts == ("ab",)


@pytest.mark.parametrize("bad", ["abc", 5, {"a": 1}, [1], [None]])
def test_list_must_be_list_of_text(bad):
    with pytest.raises(ValidationFailed) as exc:
        _create(hosts=bad)
    assert exc.value.field == "hosts"


def test_protocols_subset_and_duplicates():
    assert _create(protocols=list(KNOWN_PROTOCOLS)).protocols == KNOWN_PROTOCOLS
    assert _create(protocols=["irc", "irc", "file"]).protocols == ("irc", "file")
    for bad in (["http"], ["IRC"], [" irc"], ["irc", "x"]):
        with pytest.raises(ValidationFailed) as exc:
            _create(protocols=bad)
        assert exc.value.field == "protocols"


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationFailed) as exc:
        _create(bogus=1)
    assert exc.value.field == "bogus"


def test_code_folder_is_text_or_none_and_not_read():
    assert _create(code_folder="/x/y").code_folder == "/x/y"
    assert _create(code_folder=None).code_folder is None
    for bad in (5, "", ["a"]):
        with pytest.raises(ValidationFailed) as exc:
            _create(code_folder=bad)
        assert exc.value.field == "code_folder"


def test_patch_leaves_untouched_fields_none():
    patch = validate_service_fields({"name": "New"}, for_create=False)
    assert patch == ServicePatch(name="New")
    assert validate_service_fields({}, for_create=False) == ServicePatch()


def test_patch_code_folder_null_clears_and_other_null_is_rejected():
    assert validate_service_fields({"code_folder": None}, for_create=False).code_folder is CLEAR
    for field in ("name", "hosts", "app_names", "protocols"):
        with pytest.raises(ValidationFailed) as exc:
            validate_service_fields({field: None}, for_create=False)
        assert exc.value.field == field


def test_patch_cannot_change_id():
    with pytest.raises(ValidationFailed) as exc:
        validate_service_fields({"id": "other"}, for_create=False)
    assert exc.value.field == "id"


# ---- log sources ------------------------------------------------------------------------------

KNOWN = frozenset({"plain", "ncsa"})


def _ls(**over):
    raw = {"path": "/var/log/a.log", "format": "plain"}
    raw.update(over)
    return validate_log_source_fields(raw, known_formats=KNOWN, for_create=True)


def test_log_source_defaults():
    assert _ls() == LogSourceFields(path="/var/log/a.log", format="plain")


def test_format_must_be_known_and_default_set_is_the_five():
    with pytest.raises(ValidationFailed) as exc:
        _ls(format="xferlog")
    assert exc.value.field == "format"
    assert DEFAULT_FORMATS == {"syslog-text", "ncsa", "w3c", "xferlog", "plain"}
    got = validate_log_source_fields({"path": "/a", "format": "xferlog"}, known_formats=DEFAULT_FORMATS, for_create=True)
    assert got.format == "xferlog"
    got = validate_log_source_fields({"path": "/a", "format": "w3c"}, for_create=True)
    assert got.format == "w3c"


def test_path_and_format_required_on_create():
    for raw, field in (({"format": "plain"}, "path"), ({"path": "/a"}, "format")):
        with pytest.raises(ValidationFailed) as exc:
            validate_log_source_fields(raw, known_formats=KNOWN, for_create=True)
        assert exc.value.field == field


def test_host_is_cleaned_and_capped():
    assert _ls(host=" a\x00b ").host == "ab"
    assert _ls(host="h" * 100).host == "h" * 100
    with pytest.raises(ValidationFailed) as exc:
        _ls(host="h" * 101)
    assert exc.value.field == "host"
    assert _ls(host=None).host is None


@pytest.mark.parametrize("value", [0, 1, 500, 1000])
def test_backfill_accepts_range(value):
    assert _ls(backfill_lines=value).backfill_lines == value


@pytest.mark.parametrize("value", [-1, 1001, 1.5, "5", True, False, None])
def test_backfill_rejects(value):
    with pytest.raises(ValidationFailed) as exc:
        _ls(backfill_lines=value)
    assert exc.value.field == "backfill_lines"


def test_enabled_must_be_bool():
    assert _ls(enabled=False).enabled is False
    for bad in (1, "yes", None):
        with pytest.raises(ValidationFailed) as exc:
            _ls(enabled=bad)
        assert exc.value.field == "enabled"


def test_log_source_patch():
    patch = validate_log_source_fields({"enabled": False}, known_formats=KNOWN, for_create=False)
    assert patch == LogSourcePatch(enabled=False)
    assert validate_log_source_fields({"host": None}, known_formats=KNOWN, for_create=False).host is CLEAR
    assert validate_log_source_fields({}, known_formats=KNOWN, for_create=False) == LogSourcePatch()
    with pytest.raises(ValidationFailed):
        validate_log_source_fields({"path": "/x"}, known_formats=KNOWN, for_create=False)
    with pytest.raises(ValidationFailed):
        validate_log_source_fields({"format": "nope"}, known_formats=KNOWN, for_create=False)


def test_validators_never_read_the_filesystem(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("filesystem access")

    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setattr(os.path, "isdir", boom)
    monkeypatch.setattr(os.path, "realpath", boom)
    _create(code_folder="/some/dir", hosts=["a"])
    _ls()
    validate_service_fields({"code_folder": None}, for_create=False)
