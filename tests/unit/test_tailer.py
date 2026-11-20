"""Task 7.1: FileTailer, SourceSpec, SourceCounters, build_spec."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os

import pytest

from chimericmesh.connectors import tailer as tailer_mod
from chimericmesh.connectors.tailer import FileTailer, SourceCounters, SourceSpec, build_spec
from chimericmesh.connectors.tailread import FileIdent, PollResult, TailState
from chimericmesh.events import Event
from chimericmesh.parsers.logctx import LineFormat, ParseOutcome, identity_update, make_file_event
from chimericmesh.services.models import LogSource, Service

NCSA_500 = '203.0.113.7 - - [18/Nov/2026:10:00:01 +0000] "GET /orders/42 HTTP/1.1" 500 123\n'


class Rec:
    def __init__(self, fail_on: set[int] | None = None, hook=None) -> None:
        self.calls: list[tuple[Event, str | None]] = []
        self.fail_on = fail_on or set()
        self.hook = hook
        self.n = 0

    @property
    def events(self) -> list[Event]:
        return [e for e, _ in self.calls]

    async def __call__(self, event: Event, log_source_id: str | None = None) -> Event:
        self.n += 1
        if self.n in self.fail_on:
            raise RuntimeError("ingest failed")
        self.calls.append((event, log_source_id))
        if self.hook:
            self.hook(self.n)
        return event


async def until(pred, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def spec_for(path, fmt="plain", **kw) -> SourceSpec:
    base = dict(source_id="ls1", service_id="web", path=str(path), format=fmt, host=None, app="web", backfill_lines=0)
    base.update(kw)
    return SourceSpec(**base)


class Harness:
    def __init__(self, path, cfg, *, fmt_name="plain", rec=None, state=None, fmt=None, **spec_kw) -> None:
        self.rec = rec or Rec()
        self.counters = SourceCounters()
        self.states: list[TailState] = []
        self.spec = spec_for(path, fmt_name, **spec_kw)
        self.t = FileTailer(self.spec, state or TailState(), self.counters, self.rec, cfg, machine="m1",
                            on_state=lambda sid, st: self.states.append(st), fmt=fmt)

    @property
    def last_state(self) -> TailState:
        return self.states[-1]


def invariant(c: SourceCounters) -> None:
    assert c.lines_read == c.events_emitted + c.lines_dropped + c.skipped


# ---- build_spec -----------------------------------------------------------------------------------


def make_src(path, **kw) -> LogSource:
    base = dict(id="ls1", service_id="web", path=str(path), format="plain", enabled=True, backfill_lines=0,
                origin="user", host=None, created_at=0.0, updated_at=0.0)
    base.update(kw)
    return LogSource(**base)


def make_service(app_names=()) -> Service:
    return Service(id="web", name="web", hosts=(), app_names=tuple(app_names), protocols=(), log_sources=(),
                   code_folder=None, created_at=0.0, updated_at=0.0, seq=1)


def test_build_spec_app_from_service_first_app_name(tmp_path):
    spec = build_spec(make_src(tmp_path / "access.log"), make_service(["nginx", "other"]), "m1")
    assert spec.app == "nginx" and spec.service_id == "web" and spec.source_id == "ls1"


def test_build_spec_app_from_file_base_name_without_extension(tmp_path):
    assert build_spec(make_src(tmp_path / "access.log"), make_service(), "m1").app == "access"
    assert build_spec(make_src(tmp_path / "access.log"), None, "m1").app == "access"
    assert build_spec(make_src(tmp_path / "u_ex.log.1"), make_service(), "m1").app == "u_ex.log"


def test_build_spec_clamps_backfill(tmp_path):
    for given, want in ((5, 5), (5000, 1000), (-3, 0), (0, 0), (1000, 1000)):
        spec = build_spec(make_src(tmp_path / "a.log", backfill_lines=given), None, "m1")
        assert spec.backfill_lines == want


def test_build_spec_keeps_host_and_format(tmp_path):
    spec = build_spec(make_src(tmp_path / "a.log", host="box", format="ncsa"), None, "m1")
    assert spec.host == "box" and spec.format == "ncsa"


# ---- events ---------------------------------------------------------------------------------------


def test_ncsa_500_becomes_a_file_event_quickly(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config, fmt_name="ncsa")
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write(NCSA_500)
        t0 = asyncio.get_running_loop().time()
        await until(lambda: h.rec.calls, timeout=2.0)
        assert asyncio.get_running_loop().time() - t0 < 2.0
        await h.t.stop()
        ev, sid = h.rec.calls[0]
        assert ev.source == "file" and ev.severity == 3 and sid == "ls1"
        assert ev.host == "m1/web"
        assert h.counters.events_emitted == 1 and h.counters.lines_read == 1 and h.counters.status == "stopped"
        invariant(h.counters)

    asyncio.run(main())


def test_existing_content_is_not_emitted_on_first_attach(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("old1\nold2\n")

    async def main():
        h = Harness(log, conn_config)
        h.t.start()
        await asyncio.sleep(0.15)
        with open(log, "a") as f:
            f.write("new\n")
        await until(lambda: h.rec.calls)
        await h.t.stop()
        assert [e.message for e in h.rec.events] == ["new"]

    asyncio.run(main())


# ---- counters -------------------------------------------------------------------------------------


def test_counters_for_skipped_unparsed_and_bytes(tmp_path, conn_config):
    log = tmp_path / "w.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c")
        h.t.start()
        await asyncio.sleep(0.1)
        fields = "#Fields: date time cs-method cs-uri-stem sc-status\n"
        with open(log, "a") as f:
            f.write(fields + "\n" + "2026-11-18 10:00:00 GET /a 200\n" + "not a w3c data line\n")
        await until(lambda: h.counters.lines_read == 4)
        await h.t.stop()
        c = h.counters
        assert c.skipped == 2            # the #Fields comment and the blank line
        assert c.events_emitted == 2 and c.unparsed == 1
        assert c.bytes_read >= len(fields)
        invariant(c)

    asyncio.run(main())


def boom_format() -> LineFormat:
    def parse(line, ctx):
        if "boom" in line:
            raise ValueError("stub failure")
        return ParseOutcome(make_file_event(ctx, severity=6, message=line, raw=line), True)
    return LineFormat("plain", identity_update, parse)


def test_parse_exception_drops_one_line_and_continues(tmp_path, conn_config, caplog):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config, fmt=boom_format())
        h.t.start()
        await asyncio.sleep(0.1)
        with caplog.at_level(logging.WARNING, logger="chimericmesh.connectors"):
            with open(log, "a") as f:
                f.write("one\nboom 1\nboom 2\nthree\n")
            await until(lambda: h.counters.lines_read == 4)
        await h.t.stop()
        assert [e.message for e in h.rec.events] == ["one", "three"]
        assert h.counters.lines_dropped == 2 and h.counters.events_emitted == 2
        invariant(h.counters)
        assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 1   # once per window

    asyncio.run(main())


def test_submit_failure_counts_as_dropped_not_emitted_or_unparsed(tmp_path, conn_config):
    log = tmp_path / "w.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c", rec=Rec(fail_on={1}))
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write("garbage that is not w3c\nsecond garbage\n")
        await until(lambda: h.counters.lines_read == 2)
        await h.t.stop()
        c = h.counters
        assert c.lines_dropped == 1 and c.events_emitted == 1
        assert c.unparsed == 1                  # only the line whose submit succeeded
        invariant(c)

    asyncio.run(main())


# ---- status ---------------------------------------------------------------------------------------


def test_status_running_missing_then_running(tmp_path, conn_config):
    log = tmp_path / "late.log"

    async def main():
        h = Harness(log, conn_config)
        h.t.start()
        await until(lambda: h.counters.status == "missing")
        assert h.counters.last_error
        log.write_text("")
        await until(lambda: h.counters.status == "running")
        with open(log, "a") as f:
            f.write("first\n")
        await until(lambda: h.rec.calls)          # a file that appears later is read from its start
        await h.t.stop()
        assert h.rec.events[0].message == "first"

    asyncio.run(main())


def scripted(monkeypatch, results):
    """Replace ``poll_file`` with a script; the last item repeats. An exception item is raised."""
    calls: list[str] = []
    seq = list(results)

    def fake(path, state, **kw):
        calls.append(path)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(tailer_mod, "poll_file", fake)
    return calls


def pr(status, error="", state=None, lines=(), reason="", more=False, nbytes=0) -> PollResult:
    st = state or TailState()
    return PollResult(status, error, st, st, tuple(lines), reason, more, nbytes)


def test_unreadable_shows_os_text_cut_at_200(tmp_path, conn_config, monkeypatch):
    scripted(monkeypatch, [pr("unreadable", "x" * 500)])

    async def main():
        h = Harness(tmp_path / "a.log", conn_config)
        h.t.start()
        await until(lambda: h.counters.status == "unreadable")
        await h.t.stop()
        assert h.counters.last_error == "x" * 200

    asyncio.run(main())


def test_five_busy_polls_stay_running_the_sixth_is_unreadable(tmp_path, conn_config, monkeypatch):
    statuses: list[str] = []
    seen = {"n": 0}
    busy = pr("busy", "being used by another process")

    def fake(path, state, **kw):
        seen["n"] += 1
        return busy if seen["n"] <= 6 else pr("running")

    monkeypatch.setattr(tailer_mod, "poll_file", fake)

    async def main():
        h = Harness(tmp_path / "a.log", conn_config)
        h.t.start()
        end = asyncio.get_running_loop().time() + 3
        while seen["n"] < 7 and asyncio.get_running_loop().time() < end:
            statuses.append((seen["n"], h.counters.status))
            await asyncio.sleep(0.001)
        await h.t.stop()
        by_n = {}
        for n, s in statuses:
            by_n.setdefault(n, set()).add(s)
        for n in range(1, 6):
            assert by_n.get(n, {"running"}) <= {"running"}, (n, by_n)
        assert "unreadable" in by_n.get(6, set())

    asyncio.run(main())


def test_directory_is_error_retried_after_not_regular_retry(tmp_path):
    from chimericmesh.connectors.config import ConnectorConfig
    cfg = ConnectorConfig(poll_interval=0.01, catchup_interval=0.01, error_retry=0.01, not_regular_retry=0.2,
                          missing_retry=0.01, unreadable_retry=0.01)
    calls = {"n": 0}
    real = tailer_mod.poll_file

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    async def main():
        tailer_mod.poll_file = counting
        try:
            h = Harness(tmp_path, cfg)            # the temp directory itself
            h.t.start()
            await until(lambda: h.counters.status == "error")
            assert "not a regular file" in h.counters.last_error
            n0 = calls["n"]
            await asyncio.sleep(0.1)              # well under not_regular_retry
            assert calls["n"] == n0
            await until(lambda: calls["n"] > n0)
            await h.t.stop()
        finally:
            tailer_mod.poll_file = real

    asyncio.run(main())


def test_unexpected_exception_sets_error_waits_and_keeps_running(tmp_path, conn_config, monkeypatch):
    real = tailer_mod.poll_file
    state = {"raise": True, "n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["raise"]:
            raise RuntimeError("executor exploded")
        return real(*a, **k)

    monkeypatch.setattr(tailer_mod, "poll_file", flaky)
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config)
        task = h.t.start()
        await until(lambda: h.counters.status == "error")
        assert "executor exploded" in h.counters.last_error
        await until(lambda: state["n"] >= 3)      # retried after error_retry
        assert not task.done()
        state["raise"] = False
        await until(lambda: h.counters.status == "running")
        await h.t.stop()

    asyncio.run(main())


# ---- rate limit -----------------------------------------------------------------------------------


def test_rate_limit_drops_and_summarises_once(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config)
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write("".join(f"line {i}\n" for i in range(5000)))
        await until(lambda: h.counters.lines_read == 5000)
        assert h.counters.events_emitted == 2000 and h.counters.lines_dropped == 3000
        await until(lambda: any("rate limit" in e.message for e in h.rec.events), timeout=3.0)
        await asyncio.sleep(0.5)
        await h.t.stop()
        notes = [e for e in h.rec.events if "rate limit" in e.message]
        assert len(notes) == 1
        assert notes[0].message == "connector dropped 3000 lines (rate limit)" and notes[0].severity == 4
        assert notes[0].host == "m1/web" and notes[0].source == "file"
        assert len(h.rec.events) == 2001
        assert h.counters.events_emitted == 2000                    # the summary changes no counter
        invariant(h.counters)

    asyncio.run(main())


# ---- W3C header handling --------------------------------------------------------------------------

HDR = "#Fields: date time cs-method cs-uri-stem sc-status\n"


def test_w3c_existing_header_is_used_for_lines_appended_later(tmp_path, conn_config):
    log = tmp_path / "u.log"
    log.write_text("#Software: x\n" + HDR + "2026-11-18 10:00:00 GET /old 200\n")

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c")
        h.t.start()
        await asyncio.sleep(0.15)
        with open(log, "a") as f:
            f.write("2026-11-18 10:00:01 GET /index.aspx 500\n")
        await until(lambda: h.rec.calls)
        await h.t.stop()
        ev = h.rec.events[0]
        assert ev.message == "GET /index.aspx -> 500" and ev.severity == 3
        assert h.counters.unparsed == 0

    asyncio.run(main())


def test_w3c_resume_past_the_header_still_uses_it(tmp_path, conn_config):
    log = tmp_path / "u.log"
    log.write_text(HDR + "2026-11-18 10:00:00 GET /old 200\n")
    from chimericmesh.connectors.tailread import poll_file
    first = poll_file(str(log), TailState())
    resumed = dataclasses.replace(first.state, attach="end")

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c", state=resumed)
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write("2026-11-18 10:00:01 GET /x 503\n")
        await until(lambda: h.rec.calls)
        await h.t.stop()
        assert h.rec.events[0].message == "GET /x -> 503"

    asyncio.run(main())


def test_w3c_rotation_and_truncation_pick_up_the_new_columns(tmp_path, conn_config):
    log = tmp_path / "u.log"
    log.write_text(HDR)

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c")
        h.t.start()
        await asyncio.sleep(0.15)
        with open(log, "a") as f:
            f.write("2026-11-18 10:00:00 GET /a 500\n")
        await until(lambda: len(h.rec.calls) == 1)
        # rotation: rename away, new file with a different column order
        os.replace(log, str(log) + ".1")
        log.write_text("#Fields: sc-status cs-uri-stem cs-method date time\n404 /b GET 2026-11-18 10:00:01\n")
        await until(lambda: len(h.rec.calls) == 2)
        assert h.rec.events[1].message == "GET /b -> 404"
        # copy-truncate to a third layout
        with open(log, "r+b") as f:
            f.truncate(0)
        with open(log, "a") as f:
            f.write("#Fields: cs-uri-stem sc-status cs-method\n/c 503 POST\n")
        await until(lambda: len(h.rec.calls) == 3)
        await h.t.stop()
        assert h.rec.events[2].message == "POST /c -> 503"
        assert h.counters.unparsed == 0

    asyncio.run(main())


def test_w3c_without_header_parses_as_plain_unmatched(tmp_path, conn_config):
    log = tmp_path / "u.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config, fmt_name="w3c")
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write("2026-11-18 10:00:00 GET /a 500\n")
        await until(lambda: h.rec.calls)
        await h.t.stop()
        assert h.rec.events[0].message == "2026-11-18 10:00:00 GET /a 500"
        assert h.counters.unparsed == 1

    asyncio.run(main())


# ---- stop and restart -----------------------------------------------------------------------------


def test_stop_mid_batch_then_new_tailer_emits_each_remaining_line_once(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        holder: dict = {}
        rec = Rec(hook=lambda n: holder["h"].t._stop.set() if n == 3 else None)
        h = Harness(log, conn_config, rec=rec)
        holder["h"] = h
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "a") as f:
            f.write("".join(f"l{i}\n" for i in range(10)))
        await until(lambda: len(rec.calls) >= 3)
        await h.t.stop()
        assert [e.message for e in rec.events] == ["l0", "l1", "l2"]
        h2 = Harness(log, conn_config, rec=Rec(), state=h.last_state)
        h2.t.start()
        await until(lambda: len(h2.rec.calls) == 7)
        await asyncio.sleep(0.1)
        await h2.t.stop()
        assert [e.message for e in h2.rec.events] == [f"l{i}" for i in range(3, 10)]

    asyncio.run(main())


def test_stop_after_an_overlong_unfinished_line_leaves_discarding(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config)
        h.t.start()
        await asyncio.sleep(0.1)
        with open(log, "ab") as f:
            f.write(b"A" * 20000)
        await until(lambda: h.rec.calls)
        await h.t.stop()
        assert h.last_state.discarding is True
        h2 = Harness(log, conn_config, state=h.last_state)
        h2.t.start()
        await asyncio.sleep(0.1)
        with open(log, "ab") as f:
            f.write(b"BBBB\nnext\n")
        await until(lambda: h2.rec.calls)
        await asyncio.sleep(0.1)
        await h2.t.stop()
        assert [e.message for e in h2.rec.events] == ["next"]

    asyncio.run(main())


def test_no_ingest_after_stop_returns(tmp_path, conn_config):
    log = tmp_path / "a.log"
    log.write_text("")

    async def main():
        h = Harness(log, conn_config)
        h.t.start()
        await asyncio.sleep(0.1)
        await h.t.stop()
        n = len(h.rec.calls)
        with open(log, "a") as f:
            f.write("late\n")
        await asyncio.sleep(0.2)
        assert len(h.rec.calls) == n
        await h.t.stop()                           # twice is harmless

    asyncio.run(main())
