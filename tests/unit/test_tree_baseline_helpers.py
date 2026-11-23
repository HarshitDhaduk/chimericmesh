"""Test for ``tree_baseline`` (agent-loop task 1.5, R1-L10): hashes of working-tree bytes and their diff."""
from __future__ import annotations

import hashlib

import tree_baseline as tb


def test_snapshot_hashes_the_bytes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello\r\nworld")
    snap = tb.snapshot([str(f)])
    assert snap == {str(f): hashlib.sha256(b"hello\r\nworld").hexdigest()}


def test_missing_file_is_absent(tmp_path):
    gone = tmp_path / "nope.txt"
    assert tb.snapshot([str(gone)]) == {str(gone): "absent"}


def test_directory_is_not_a_file(tmp_path):
    assert tb.snapshot([str(tmp_path)]) == {str(tmp_path): "absent"}


def test_relative_paths_resolve_against_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_bytes(b"x")
    snap = tb.snapshot(["sub/x.py", "sub/y.py"], root=str(tmp_path))
    assert snap["sub/x.py"] == hashlib.sha256(b"x").hexdigest()
    assert snap["sub/y.py"] == "absent"


def test_diff_snapshots_lists_changed_added_removed(tmp_path):
    a = {"a": "1", "b": "2", "c": "3"}
    b = {"a": "1", "b": "9", "d": "4"}
    assert tb.diff_snapshots(a, b) == ["b", "c", "d"]
    assert tb.diff_snapshots(a, dict(a)) == []


def test_a_content_change_shows_in_the_diff(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"one")
    before = tb.snapshot([str(f)])
    f.write_bytes(b"two")
    assert tb.diff_snapshots(before, tb.snapshot([str(f)])) == [str(f)]
    f.unlink()
    assert tb.diff_snapshots(before, tb.snapshot([str(f)])) == [str(f)]
