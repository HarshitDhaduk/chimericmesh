"""Environment and wildcard expansion (R2.2, R3.2, R3.6)."""
from __future__ import annotations

import pytest

from chimericmesh.discovery.expand import ALLOWED_ENV, expand_leading_env, expand_pattern


class FakeFs:
    """Directory tree: absolute directory -> entry names. Records every list call."""

    def __init__(self, tree: dict[str, list[str]], ci: bool = False) -> None:
        self.tree = tree
        self.calls: list[tuple[str, int]] = []

    def __call__(self, directory: str, limit: int):
        self.calls.append((directory, limit))
        names = list(self.tree.get(directory, []))
        return names[:limit], len(names) > limit


IIS = "%SystemDrive%\\inetpub\\logs\\LogFiles\\W3SVC*\\*.log"
WIN_ENV = {"SystemDrive": "C:", "SystemRoot": "C:\\Windows", "ProgramData": "C:\\ProgramData"}


def test_windows_pattern_expands_two_wildcard_levels_case_insensitively():
    fs = FakeFs({
        "C:\\inetpub\\logs\\LogFiles": ["W3SVC1", "w3svc2", "other"],
        "C:\\inetpub\\logs\\LogFiles\\W3SVC1": ["u_ex1.log", "U_EX2.LOG", "x.txt"],
        "C:\\inetpub\\logs\\LogFiles\\w3svc2": ["a.log"],
    })
    res = expand_pattern(IIS, "windows", WIN_ENV, fs)
    assert res.paths == (
        "C:\\inetpub\\logs\\LogFiles\\W3SVC1\\U_EX2.LOG",
        "C:\\inetpub\\logs\\LogFiles\\W3SVC1\\u_ex1.log",
        "C:\\inetpub\\logs\\LogFiles\\w3svc2\\a.log",
    )
    assert not res.truncated


def test_linux_home_pattern():
    fs = FakeFs({"/home/u/logs": ["a.log", "b.txt", "c.log"]})
    res = expand_pattern("$HOME/logs/*.log", "linux", {"HOME": "/home/u"}, fs)
    assert res.paths == ("/home/u/logs/a.log", "/home/u/logs/c.log")


def test_literal_patterns_need_no_lister():
    fs = FakeFs({})
    assert expand_pattern("/var/log/vsftpd.log", "linux", {}, fs).paths == ("/var/log/vsftpd.log",)
    assert expand_pattern("C:\\Apache24\\logs\\access.log", "windows", {}, fs).paths == (
        "C:\\Apache24\\logs\\access.log",)
    assert fs.calls == []


@pytest.mark.parametrize("pattern,os_name", [
    ("%PATH%\\x\\*.log", "windows"),
    ("$SECRET/x/*.log", "linux"),
    ("%Nope%\\x", "windows"),
    ("relative/path/*.log", "linux"),
    ("C:\\a\\%SystemDrive%\\b", "windows"),
    ("/var/$HOME/log", "linux"),
    ("/var/../etc/*.log", "linux"),
])
def test_bad_patterns_yield_nothing(pattern, os_name):
    fs = FakeFs({})
    assert expand_pattern(pattern, os_name, {"HOME": "/h", "SECRET": "/s", "PATH": "/p"}, fs).paths == ()
    assert fs.calls == []


@pytest.mark.parametrize("env", [
    {},                                          # missing
    {"SystemDrive": "relative"},                 # not absolute
    {"SystemDrive": "C:\x00"},                   # control character
    {"SystemDrive": "C:\n"},
    {"SystemDrive": ""},
    {"SystemDrive": 5},
])
def test_bad_environment_values_yield_nothing(env):
    assert expand_leading_env("%SystemDrive%\\x", "windows", env) is None
    assert expand_pattern("%SystemDrive%\\x", "windows", env, FakeFs({})).paths == ()


def test_placeholder_must_be_at_the_start():
    assert expand_leading_env("x\\%SystemDrive%\\y", "windows", WIN_ENV) is None
    assert expand_leading_env("/x/$HOME", "linux", {"HOME": "/h"}) is None


def test_env_value_is_a_literal_base_never_rescanned():
    fs = FakeFs({"/tmp/*": ["a.log"], "/tmp": ["a.log"]})
    res = expand_pattern("$HOME/*.log", "linux", {"HOME": "/tmp/*"}, fs)
    assert [c[0] for c in fs.calls] == ["/tmp/*"]          # only the literal base is listed
    assert res.paths == ("/tmp/*/a.log",)
    fs2 = FakeFs({"C:\\%ProgramData%\\z": ["q.log"]})
    res2 = expand_pattern("%SystemDrive%\\*.log", "windows",
                          {"SystemDrive": "C:\\%ProgramData%\\z", "ProgramData": "C:\\PD"}, fs2)
    assert [c[0] for c in fs2.calls] == ["C:\\%ProgramData%\\z"]
    assert res2.paths == ("C:\\%ProgramData%\\z\\q.log",)


def test_lister_is_never_called_outside_the_literal_base():
    fs = FakeFs({"/var/log": ["nginx", "apache2"], "/var/log/nginx": ["access.log"]})
    expand_pattern("/var/log/nginx/*.log", "linux", {}, fs)
    assert all(c[0].startswith("/var/log/nginx") for c in fs.calls)


def test_at_most_200_names_are_listed_per_pattern():
    names = [f"f{i:05d}.log" for i in range(10_000)]
    fs = FakeFs({"/var/log/x": names})
    res = expand_pattern("/var/log/x/*.log", "linux", {}, fs)
    assert len(res.paths) <= 200
    assert res.truncated is True
    assert all(limit <= 200 for _, limit in fs.calls)


def test_total_listing_budget_spans_directories():
    tree = {"/d": [f"s{i}" for i in range(150)]}
    for i in range(150):
        tree[f"/d/s{i}"] = [f"a{j}.log" for j in range(150)]
    fs = FakeFs(tree)
    res = expand_pattern("/d/*/*.log", "linux", {}, fs)
    assert sum(len(v) for v in [fs.tree.get(c[0], [])[: c[1]] for c in fs.calls]) <= 200
    assert len(res.paths) <= 200 and res.truncated


def test_names_with_separators_or_control_characters_are_skipped():
    fs = FakeFs({"/l": ["ok.log", "a\x00.log", "b\n.log", "c\\d.log", "e/f.log", "\x1b.log"]})
    res = expand_pattern("/l/*.log", "linux", {}, fs)
    # both separators are skipped on every OS (conservative), control characters are skipped, not cleaned
    assert res.paths == ("/l/ok.log",)


def test_results_are_sorted_and_deterministic():
    fs = FakeFs({"/l": ["c.log", "a.log", "b.log"]})
    first = expand_pattern("/l/*.log", "linux", {}, fs).paths
    fs2 = FakeFs({"/l": ["b.log", "c.log", "a.log"]})
    assert first == expand_pattern("/l/*.log", "linux", {}, fs2).paths == ("/l/a.log", "/l/b.log", "/l/c.log")


def test_path_syntax_follows_os_name_not_host():
    fs = FakeFs({"/l": ["a.log"]})
    assert expand_pattern("/l/*.log", "macos", {}, fs).paths == ("/l/a.log",)
    fsw = FakeFs({"C:\\l": ["a.log"]})
    assert expand_pattern("C:\\l\\*.log", "windows", {}, fsw).paths == ("C:\\l\\a.log",)
    assert expand_pattern("C:/l/*.log", "windows", {}, fsw).paths == ("C:\\l\\a.log",)


def test_question_mark_and_bracket_are_literal_safe():
    fs = FakeFs({"/l": ["a1.log", "ab.log", "[x].log"]})
    assert expand_pattern("/l/a?.log", "linux", {}, fs).paths == ("/l/a1.log", "/l/ab.log")
    assert expand_pattern("/l/[x].log", "linux", {}, fs).paths == ("/l/[x].log",)


def test_allowed_env_lists():
    assert ALLOWED_ENV["windows"] == ("ProgramData", "SystemDrive", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
    assert ALLOWED_ENV["linux"] == ("HOME",) and ALLOWED_ENV["macos"] == ("HOME",)


def test_windows_environment_lookup_is_case_insensitive():
    # os.environ keys are upper case on Windows
    assert expand_leading_env("%SystemDrive%\\x", "windows", {"SYSTEMDRIVE": "C:"}) == ("C:\\", "\\x")
    assert expand_leading_env("%SYSTEMDRIVE%\\x", "windows", {"SystemDrive": "D:"}) == ("D:\\", "\\x")


def test_leading_env_returns_rest_unchanged():
    assert expand_leading_env("$HOME/a/*.log", "linux", {"HOME": "/h/"}) == ("/h", "/a/*.log")
    assert expand_leading_env("%ProgramFiles(x86)%\\FileZilla Server\\Logs\\*.log", "windows",
                              {"ProgramFiles(x86)": "C:\\Program Files (x86)"}) == (
        "C:\\Program Files (x86)", "\\FileZilla Server\\Logs\\*.log")


@pytest.mark.parametrize("value", ["/\\", "\\", "/", "relative\\x", "C:", "\\\\", "\\\\server", "\\\\server\\"])
def test_windows_malformed_env_value_gives_no_expansion(value):
    assert expand_leading_env("%PROGRAMDATA%", "windows", {"ProgramData": value}) is None


@pytest.mark.parametrize("value, base", [("C:\\ProgramData", "C:\\ProgramData"),
                                         ("\\\\server\\share", "\\\\server\\share")])
def test_windows_absolute_env_value_still_expands(value, base):
    assert expand_leading_env("%PROGRAMDATA%\\x", "windows", {"ProgramData": value}) == (base, "\\x")


@pytest.mark.parametrize("value", ["relative", "", "\\x"])
def test_posix_env_value_must_start_with_slash(value):
    assert expand_leading_env("$HOME/x", "linux", {"HOME": value}) is None
