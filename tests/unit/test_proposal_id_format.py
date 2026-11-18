"""Guard: proposal ids stay ``p<number>``, and the README says what the data layer does."""
import re
from pathlib import Path

from chimericmesh.store.config import PROPOSAL_ID_NOTE, describe_startup

ROOT = Path(__file__).resolve().parents[2]
# Built from fragments so this file never matches its own pattern.
PATTERN = re.compile(r"\b" + "p" + r"\d+-" + r"[a-z0-9]{3,}" + r"\b")
SCAN_DIRS = ("src", "tests", "powers", ".kiro/agents")
SKIP_PARTS = {"__pycache__", ".hypothesis", ".pytest_cache"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".toml", ".html", ".js", ".yml", ".yaml"}

# Each entry is a legitimate match found by the scan, with the reason it is not a proposal id.
ALLOWED: tuple[str, ...] = ()


def scanned_files():
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (path.is_file() and path.suffix in TEXT_SUFFIXES and not SKIP_PARTS & set(path.parts)
                    and path.resolve() != Path(__file__).resolve()):
                yield path


def test_no_proposal_id_carries_a_tag():
    found = []
    for path in scanned_files():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for m in PATTERN.finditer(line):
                if m.group(0) not in ALLOWED:
                    found.append(f"{path.relative_to(ROOT)}:{n}: {m.group(0)}")
    assert found == []


def test_the_pattern_matches_tagged_ids_and_ignores_ranges():
    assert PATTERN.search("see p12-abc for details")
    for fine in ("p1-p5", "p1-3", "p10-ab", "P1-abc", "up1-abc"):
        assert not PATTERN.search(fine), fine


def readme() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", text.replace("`", "")).lower()


def has(phrase: str) -> bool:
    return re.sub(r"\s+", " ", phrase.replace("`", "")).lower() in readme()


def test_readme_states_proposal_id_scope():
    assert has(PROPOSAL_ID_NOTE)
    assert "p1" in PROPOSAL_ID_NOTE


def test_startup_line_prints_the_path_and_the_statement():
    line = describe_startup("/data/chimericmesh.db")
    assert "/data/chimericmesh.db" in line and PROPOSAL_ID_NOTE in line
    assert "memory" in describe_startup(":memory:").lower()


def test_readme_documents_relative_db_and_event_id_risk():
    assert has("an explicit relative CHIMERICMESH_DB is resolved once against the current directory")
    assert has("the defaults never depend on the current directory")
    assert has("after a crash that follows a long storage outage, a restart may reuse event ids a client saw")


def test_readme_documents_single_process_and_crash_marker():
    assert has("one database file supports one running hub process")
    assert has("two processes serving one file are not supported")
    assert has("a start that fails before serving leaves the crash marker untouched")


def test_readme_documents_newer_schema_refusal():
    assert has("refusing a newer-schema database after a crash may still checkpoint its -wal file into the "
               "database file")
    assert has("the data is not changed")


def test_readme_lists_environment_variables_and_reserved_prefixes():
    for name in ("CHIMERICMESH_DB", "CHIMERICMESH_HOME", "CHIMERICMESH_MAX_EVENTS",
                 "CHIMERICMESH_EVENT_TTL_DAYS", "CHIMERICMESH_REOPEN_AFTER_SECONDS"):
        assert name.lower() in readme(), name
    for prefix in ("rule:", "rule-", "withdrawn:", "expired:", "expired-"):
        assert prefix in readme(), prefix


def test_readme_no_longer_claims_proposals_live_only_in_memory():
    assert "proposals live in memory" not in readme()
