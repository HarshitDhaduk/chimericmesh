"""Limits and safe text of the agent loop (R1.2, R1.3, R1.4, R1.7, R4.2, R4.5, R1-M6)."""
from __future__ import annotations

import json
import logging

from chimericmesh.agent import limits
from chimericmesh.agent.limits import cap_items, clamp, clean_text, watch_max_wait
from chimericmesh.approvals.models import ApprovalSettings


def test_constants_equal_the_requirement_numbers():
    assert limits.MAX_FIELD == 500
    assert limits.MAX_RESPONSE_BYTES == 20_480
    assert (limits.MAX_EVENTS, limits.DEFAULT_EVENTS) == (100, 25)
    assert limits.MAX_MINUTES == 240
    assert limits.MAX_QUERY == 100
    assert limits.MAX_HITS == 10
    assert limits.MAX_FILES_SCANNED == 2000
    assert limits.MAX_FILE_BYTES == 512 * 1024
    assert limits.SEARCH_SECONDS == 5.0
    assert (limits.WAIT_MIN, limits.TICK, limits.MAX_WAITERS) == (1, 0.5, 4)
    assert limits.WAIT_CEILING == 25
    assert (limits.MAX_EXCLUDE, limits.MAX_ID_LEN) == (200, 40)


def test_rule_approval_window_equals_the_auto_approval_setting():
    assert limits.RULE_APPROVAL_WINDOW == ApprovalSettings.from_env({}).rule_approval_ttl == 1800


def test_clean_text_removes_control_characters():
    dirty = "a\x00b\rc\nd\x1be\x7ff\x85g"
    assert clean_text(dirty) == "abcdefg"


def test_clean_text_removes_lone_surrogates_and_keeps_text():
    assert clean_text("x\ud800y\udfffz") == "xyz"
    assert clean_text("caf\u00e9 \u4e2d\u6587") == "caf\u00e9 \u4e2d\u6587"


def test_clean_text_cuts_to_the_limit():
    assert len(clean_text("a" * 900)) == 500
    assert clean_text("a" * 900, 200) == "a" * 200
    assert clean_text("abc", 0) == ""


def test_clean_text_accepts_none_and_int():
    assert clean_text(None) == ""
    assert clean_text(42) == "42"


def test_clamp():
    assert clamp(5, 1, 100) == 5
    assert clamp(-5, 1, 100) == 1
    assert clamp(10_000, 1, 100) == 100
    assert clamp(0, 0, 0) == 0


def _items(n: int, text: str = "x") -> list[dict]:
    return [{"id": i, "message": text} for i in range(n)]


def test_cap_items_keeps_all_when_small():
    out = cap_items({"service_id": "s"}, "events", _items(3))
    assert out["events"] == _items(3)
    assert "truncated" not in out or out["truncated"] is False


def test_cap_items_drops_from_the_tail_and_sets_truncated():
    out = cap_items({"service_id": "s"}, "events", _items(2000, "y" * 100))
    assert out["truncated"] is True
    kept = out["events"]
    assert 0 < len(kept) < 2000
    assert kept == _items(2000, "y" * 100)[: len(kept)]
    raw = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 20_480
    # one more item would not have fitted
    bigger = dict(out, events=_items(2000, "y" * 100)[: len(kept) + 1])
    assert len(json.dumps(bigger, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 20_480


def test_cap_items_measures_utf8_bytes():
    item = {"message": "\u4e2d" * 100}     # 300 bytes each
    out = cap_items({}, "events", [dict(item) for _ in range(100)])
    raw = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 20_480
    assert out["truncated"] is True
    kept = len(out["events"])
    assert kept > 60                      # about 20 KB / 301 bytes, not 20 KB / 100 characters
    more = dict(out, events=[dict(item) for _ in range(kept + 1)])
    assert len(json.dumps(more, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 20_480


def test_cap_items_multibyte_list_that_fits_stays_whole():
    items = [{"m": "\u00e9" * 20} for _ in range(100)]    # 100 * ~60 bytes
    out = cap_items({}, "events", items)
    assert len(out["events"]) == 100
    assert not out.get("truncated")


def test_cap_items_empty_list_fits_and_is_valid_json():
    out = cap_items({"a": 1}, "events", [])
    assert out["events"] == []
    assert json.loads(json.dumps(out)) == out
    assert not out.get("truncated")


def test_watch_max_wait_defaults_and_lowering_only():
    assert watch_max_wait({}) == 25
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": "10"}) == 10
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": "99"}) == 25
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": "0"}) == 1
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": "-4"}) == 1
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": " 7 "}) == 7
    assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": ""}) == 25


def test_watch_max_wait_bad_value_logs_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="chimericmesh.agent.limits"):
        assert watch_max_wait({"CHIMERICMESH_WATCH_MAX_WAIT": "abc"}) == 25
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "abc" not in warnings[0].getMessage() or len(warnings[0].getMessage()) < 200


def test_wait_max_is_within_bounds():
    assert limits.WAIT_MIN <= limits.WAIT_MAX <= limits.WAIT_CEILING
