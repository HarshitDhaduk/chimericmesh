import logging
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chimericmesh import fsperms


class FakeRunner:
    def __init__(self, returncode=0, exc=None):
        self.calls = []
        self.returncode = returncode
        self.exc = exc

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def test_posix_modes_with_fake_chmod(tmp_path):
    modes = []
    fsperms.secure_dir(tmp_path, os_name="posix", chmod=lambda p, m: modes.append((str(p), m)))
    fsperms.secure_file(tmp_path / "f", os_name="posix", chmod=lambda p, m: modes.append((str(p), m)))
    assert [m for _, m in modes] == [0o700, 0o600]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_posix_real_modes(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = tmp_path / "f"
    f.write_text("x")
    fsperms.secure_dir(d)
    fsperms.secure_file(f)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_windows_command_for_directory_and_file(tmp_path):
    runner = FakeRunner()
    assert fsperms.secure_dir(tmp_path, os_name="nt", runner=runner, user="alice") is True
    assert fsperms.secure_file(tmp_path / "f.db", os_name="nt", runner=runner, user="alice") is True
    (dir_cmd, dir_kw), (file_cmd, file_kw) = runner.calls
    assert dir_cmd == ["icacls", os.path.abspath(tmp_path), "/inheritance:r", "/grant:r", "alice:(OI)(CI)F"]
    assert file_cmd[-1] == "alice:F"
    for kw in (dir_kw, file_kw):
        assert kw.get("shell", False) is False
        assert kw["timeout"] == 10


@pytest.mark.parametrize("user", ["a;b&c", "x|y", 'q"r', "a`b", "", "a\nb", "$(whoami)x>"])
def test_windows_rejects_unsafe_user_names(tmp_path, user, caplog):
    runner = FakeRunner()
    with caplog.at_level(logging.WARNING):
        assert fsperms.secure_dir(tmp_path, os_name="nt", runner=runner, user=user) is False
    assert runner.calls == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize("user", ["alice", "DOMAIN\\alice", "john doe", "a.b-c_d", "svc$", "me@corp"])
def test_windows_accepts_plain_user_names(tmp_path, user):
    runner = FakeRunner()
    assert fsperms.secure_file(tmp_path / "f", os_name="nt", runner=runner, user=user) is True
    assert runner.calls[0][0][-1] == f"{user}:F"


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(exc=FileNotFoundError("icacls")),
        FakeRunner(exc=subprocess.TimeoutExpired("icacls", 10)),
        FakeRunner(exc=OSError("boom")),
        FakeRunner(returncode=5),
    ],
)
def test_failing_or_missing_icacls_warns_once_and_does_not_raise(tmp_path, runner, caplog):
    with caplog.at_level(logging.WARNING):
        assert fsperms.secure_file(tmp_path / "f", os_name="nt", runner=runner, user="alice") is False
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_unsupported_platform_warns_once(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert fsperms.secure_dir(tmp_path, os_name="java") is False
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_chmod_failure_warns_not_raises(tmp_path, caplog):
    def boom(p, m):
        raise PermissionError("no")

    with caplog.at_level(logging.WARNING):
        assert fsperms.secure_file(tmp_path / "f", os_name="posix", chmod=boom) is False
    assert len(caplog.records) == 1


def test_secure_sidecars_only_touches_existing_files(tmp_path):
    db = tmp_path / "x.db"
    (tmp_path / "x.db-wal").write_text("")
    seen = []
    fsperms.secure_sidecars(db, os_name="posix", chmod=lambda p, m: seen.append(os.path.basename(p)))
    assert seen == ["x.db-wal"]


# Manual check (not automated): on Windows run `icacls <home>` after the first start and confirm that
# only the current user has access and that inheritance is removed.
