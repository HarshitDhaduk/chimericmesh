"""Public code-folder names in core (auto-approval L4) and the golden behaviour of find_code_hints."""
from __future__ import annotations

from pathlib import Path

from chimericmesh import diagnostics
from chimericmesh.diagnostics import Incident, find_code_hints


def test_public_sets_are_frozensets_with_todays_values():
    assert isinstance(diagnostics.CODE_SUFFIXES, frozenset)
    assert isinstance(diagnostics.SKIP_DIRS, frozenset)
    assert diagnostics.CODE_SUFFIXES == {".py", ".js", ".ts", ".c", ".h", ".go", ".java", ".rb", ".sh", ".rs"}
    assert diagnostics.SKIP_DIRS == {".git", "node_modules", "__pycache__", ".venv", "venv", ".hypothesis"}


def test_private_aliases_still_exist():
    assert diagnostics._CODE_SUFFIXES == diagnostics.CODE_SUFFIXES
    assert diagnostics._SKIP_DIRS == diagnostics.SKIP_DIRS


def test_find_code_hints_golden(tmp_path: Path):
    (tmp_path / "a.py").write_text('log("disk is totally full here")\n')
    (tmp_path / "b.php").write_text('log("disk is totally full here")\n')
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.py").write_text('log("disk is totally full here")\n')
    inc = Incident(id="i1", host="h", category="disk", signature="s", severity=3, count=3, first_seen=0.0,
                   last_seen=0.0, sample="disk is totally full here")
    found = find_code_hints(inc, tmp_path)
    assert [h["file"] for h in found] == ["a.py"]
