"""Code folder and log path rules (R2.1 to R2.3, R3.2 to R3.4)."""
from __future__ import annotations

import builtins
import os
import stat

import pytest

from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.paths import (
    inspect_log_path,
    is_too_broad,
    normalize_code_folder,
    normalize_log_path,
    path_in_code_folder,
)

WIN = os.name == "nt"


def _mkdir(base, *parts):
    p = base.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted here")


@pytest.fixture
def world(tmp_path):
    home = _mkdir(tmp_path, "home", "me")
    data = _mkdir(home, ".chimericmesh")
    proj = _mkdir(home, "proj")
    return {"tmp": tmp_path, "home": str(home), "data": str(data), "proj": proj}


def _norm(world, raw):
    return normalize_code_folder(raw, protected=[world["data"]], home=world["home"])


# ---- normalize_code_folder ---------------------------------------------------------------------

def test_returns_resolved_absolute_path(world):
    proj = world["proj"]
    expected = os.path.realpath(proj)
    sep = os.sep
    other = "/" if sep == "\\" else "\\"
    spellings = [
        str(proj),
        str(proj) + sep,
        os.path.join(str(proj.parent), ".", "proj"),
        str(proj).replace(sep, sep + sep),
        str(proj).replace(sep, other) if WIN else str(proj),
    ]
    for raw in spellings:
        assert _norm(world, raw) == expected, raw


def test_symlink_is_resolved(world):
    link = world["tmp"] / "link"
    _symlink_or_skip(link, world["proj"])
    assert _norm(world, str(link)) == os.path.realpath(world["proj"])


@pytest.mark.skipif(not WIN, reason="case folding is a Windows rule")
def test_different_case_gives_same_path(world):
    assert _norm(world, str(world["proj"]).swapcase()) == os.path.realpath(world["proj"])


def test_idempotent(world):
    once = _norm(world, str(world["proj"]))
    assert _norm(world, once) == once


@pytest.mark.parametrize("tail", ["a/../a", "..\\x", "a/..", "a\\..\\a", "../a"])
def test_any_dotdot_component_is_rejected(world, tail):
    _mkdir(world["tmp"], "a")
    for text in (tail, str(world["tmp"]) + os.sep + tail, str(world["tmp"]) + "/" + tail):
        with pytest.raises(ValidationFailed) as exc:
            _norm(world, text)
        assert exc.value.field == "code_folder"


def test_dotdot_rejected_even_when_the_result_is_fine(world):
    raw = os.path.join(str(world["proj"]), "..", "proj")
    assert os.path.isdir(raw)
    with pytest.raises(ValidationFailed) as exc:
        _norm(world, raw)
    assert "parent" in exc.value.reason


def test_rejects_relative_missing_file_nul_and_long(world):
    f = world["tmp"] / "afile"
    f.write_text("x")
    cases = [
        "relative/dir", "", str(world["tmp"] / "does-not-exist"), str(f),
        str(world["proj"]) + "\x00", str(world["tmp"] / ("d" * 1100)),
    ]
    for raw in cases:
        with pytest.raises(ValidationFailed) as exc:
            _norm(world, raw)
        assert exc.value.field == "code_folder", raw
    with pytest.raises(ValidationFailed):
        normalize_code_folder(5, protected=[], home=world["home"])  # type: ignore[arg-type]


def test_rejects_root_home_parent_of_home_and_data_dir(world):
    root = os.path.abspath(os.sep)
    parent_of_home = os.path.dirname(world["home"])
    inside_data = _mkdir(world["tmp"], "home", "me", ".chimericmesh", "sub")
    for raw in (root, world["home"], parent_of_home, world["data"], str(inside_data)):
        with pytest.raises(ValidationFailed) as exc:
            _norm(world, raw)
        assert exc.value.field == "code_folder", raw


def test_sibling_of_home_is_accepted(world):
    sibling = _mkdir(world["tmp"], "home", "sibling")
    assert _norm(world, str(sibling)) == os.path.realpath(sibling)


def test_folder_containing_a_protected_dir_is_rejected(world, tmp_path):
    other = _mkdir(tmp_path, "elsewhere", "inner")
    parent = other.parent
    with pytest.raises(ValidationFailed):
        normalize_code_folder(str(parent), protected=[str(other)], home=world["home"])


def test_is_too_broad_reasons(world):
    assert is_too_broad(os.path.abspath(os.sep), protected=[], home=world["home"])
    assert is_too_broad(world["home"], protected=[], home=world["home"])
    assert is_too_broad(os.path.dirname(world["home"]), protected=[], home=world["home"])
    assert is_too_broad(world["data"], protected=[world["data"]], home=world["home"])
    assert is_too_broad(str(world["proj"]), protected=[world["data"]], home=world["home"]) is None


# ---- path_in_code_folder -----------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path):
    f = _mkdir(tmp_path, "code")
    (f / "src").mkdir()
    return str(f)


def test_children_are_inside(folder):
    assert path_in_code_folder(folder, "src/a.py")
    assert path_in_code_folder(folder, "src\\a.py")
    assert path_in_code_folder(folder, "a.py")
    assert path_in_code_folder(folder, os.path.join(folder, "src", "a.py"))
    assert path_in_code_folder(folder, "src/../b.py")


def test_folder_itself_and_escapes_are_outside(folder):
    for cand in (
        ".", "", folder, "..\\x", "../x", "src/../../x", "src\\..\\..\\x",
        os.path.join(os.path.dirname(folder), "other", "f"), os.path.abspath(os.sep),
        "a\x00b", folder + "\x00",
    ):
        assert not path_in_code_folder(folder, cand), repr(cand)


def test_sibling_with_common_prefix_is_outside(folder):
    assert not path_in_code_folder(folder, folder + "-evil" + os.sep + "f")


@pytest.mark.skipif(not WIN, reason="Windows name rules")
def test_windows_case_and_name_tricks(folder):
    assert path_in_code_folder(folder.upper(), "SRC/A.PY")
    for cand in ("src/a.py.", "src/a.py ", "src /a.py", "C:x", "a.py:stream", "src/..." ):
        assert not path_in_code_folder(folder, cand), cand


@pytest.mark.skipif(WIN, reason="different case is a different file on POSIX")
def test_posix_case_is_significant(folder):
    assert folder.upper() == folder or not path_in_code_folder(folder.upper(), os.path.join(folder, "a.py"))


def test_injected_realpath_models_symlinks(folder):
    outside = os.path.join(os.path.dirname(folder), "outside")
    inside = os.path.join(folder, "real")
    link_out = os.path.join(folder, "src", "link")
    link_in = os.path.join(folder, "src", "okay")

    def rp(p):
        p = os.path.normpath(p)
        if p == link_out or p.startswith(link_out + os.sep):
            return outside + p[len(link_out):]
        if p == link_in or p.startswith(link_in + os.sep):
            return inside + p[len(link_in):]
        return p

    assert not path_in_code_folder(folder, "src/link", realpath=rp)
    assert not path_in_code_folder(folder, "src/link/x.py", realpath=rp)
    assert path_in_code_folder(folder, "src/okay/x.py", realpath=rp)


def test_real_symlink_leaving_the_folder(folder, tmp_path):
    outside = _mkdir(tmp_path, "outside")
    _symlink_or_skip(os.path.join(folder, "src", "leave"), outside)
    assert not path_in_code_folder(folder, "src/leave/x.py")


def test_realpath_failure_is_false(folder):
    def boom(_p):
        raise OSError("nope")

    assert path_in_code_folder(folder, "a.py", realpath=boom) is False


# ---- log paths ---------------------------------------------------------------------------------

def test_normalize_log_path(tmp_path):
    p = str(tmp_path / "a" / ".." / "b.log")
    assert normalize_log_path(p) == os.path.normpath(p)
    for bad in ("rel/b.log", "", str(tmp_path) + "\x00", str(tmp_path / ("x" * 1100)), 5):
        with pytest.raises(ValidationFailed) as exc:
            normalize_log_path(bad)  # type: ignore[arg-type]
        assert exc.value.field == "path"


def test_inspect_accepts_missing_regular_and_permission_error(tmp_path, monkeypatch):
    inspect_log_path(str(tmp_path / "missing.log"))
    f = tmp_path / "a.log"
    f.write_text("x")
    inspect_log_path(str(f))

    def deny(*a, **k):
        raise PermissionError("no")

    monkeypatch.setattr(os, "stat", deny)
    inspect_log_path(str(f))


def test_inspect_rejects_directory(tmp_path):
    with pytest.raises(ValidationFailed) as exc:
        inspect_log_path(str(tmp_path))
    assert exc.value.field == "path"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_inspect_rejects_fifo(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValidationFailed):
        inspect_log_path(str(fifo))


def test_inspect_rejects_device_modes(tmp_path, monkeypatch):
    for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
        fake = os.stat_result((mode | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        monkeypatch.setattr(os, "stat", lambda *_a, _f=fake, **_k: _f)
        with pytest.raises(ValidationFailed):
            inspect_log_path(str(tmp_path / "x"))


def test_log_path_functions_never_open_the_file(tmp_path, monkeypatch):
    f = tmp_path / "a.log"
    f.write_text("x")

    def no_open(*a, **k):
        raise AssertionError("opened a file")

    monkeypatch.setattr(builtins, "open", no_open)
    monkeypatch.setattr(os, "open", no_open)
    inspect_log_path(normalize_log_path(str(f)))
