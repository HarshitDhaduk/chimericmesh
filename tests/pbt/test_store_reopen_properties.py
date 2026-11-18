"""Properties 12, 13 and 14: the reopen rule, driven the way the hub drives it."""
import asyncio

from hypothesis import given, strategies as st

from chimericmesh.diagnostics import IncidentTracker
from chimericmesh.events import Event
from chimericmesh.store.incidents import IncidentLifecycle
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "disk write failed: No space left on device"

ops = st.lists(
    st.one_of(
        st.tuples(st.just("event"), st.integers(2, 4)),
        st.tuples(st.just("resolve"), st.just(0)),
        st.tuples(st.just("advance"), st.integers(0, 700)),
    ),
    max_size=40,
)


class Driver:
    """Mirrors the hub's ingest rule for one incident (no hub import: the store group stays below it)."""

    def __init__(self, reopen_after: int, store):
        self.t = 10_000.0
        self.lines: list[str] = []
        self.tracker = IncidentTracker()
        self.announced: dict[str, int] = {}

        async def announce(text: str) -> None:
            self.lines.append(text)

        self.lc = IncidentLifecycle(self.tracker, store=store, announce=announce, now=lambda: self.t,
                                    reopen_after=reopen_after, announced_getter=self.announced.get)
        self.violations: list[str] = []

    async def event(self, sev: int) -> None:
        ev = Event("syslog", "h1", "app", sev, MSG, MSG, self.t)
        iid = self.tracker.preview_id(ev)
        inc = self.tracker._clusters.get(iid)
        if inc is not None and inc.resolved:
            gap = self.t - inc.resolved_at
            before = (inc.opening, inc.count)
            if self.lc.reopen_due(inc):
                if gap < self.lc.reopen_after:
                    self.violations.append("reopened inside the window")
                self.tracker.reopen(inc, ev)
                self.announced[inc.id] = 1
                await self.lc.reopen(inc)
                if not (inc.opening == before[0] + 1 and inc.count == 1 and inc.opened
                        and inc.severity == sev and not inc.resolved and inc.resolved_at is None
                        and self.tracker.get(inc.id) is inc):
                    self.violations.append("reopen did not reset state")
            else:
                if gap >= self.lc.reopen_after:
                    self.violations.append("did not reopen after the window")
                self.tracker.add(ev)
                if not (inc.resolved and inc.count == before[1] + 1 and inc.opening == before[0]):
                    self.violations.append("window event changed the wrong state")
            return
        inc = self.tracker.add(ev)
        if inc is not None and inc.opened and inc.id not in self.announced:
            self.announced[inc.id] = inc.count
            await self.lc.persist(inc)
            self.lines.append(f"[SEV] {inc.id}")

    def resolve(self) -> None:
        for inc in self.tracker.incidents():
            if not inc.resolved:
                inc.resolved, inc.resolved_at = True, self.t


async def run(reopen_after: int, sequence, store) -> Driver:
    d = Driver(reopen_after, store)
    for op, arg in sequence:
        if op == "event":
            await d.event(arg)
        elif op == "resolve":
            d.resolve()
        else:
            d.t += arg
    return d


def check(reopen_after: int, sequence, with_store: bool) -> None:
    store = Store.open(":memory:", registry=core_registry()) if with_store else None
    try:
        d = asyncio.run(run(reopen_after, sequence, store))
    finally:
        if store is not None:
            store.close()
    assert d.violations == []
    incs = [i for i in d.tracker.incidents()]
    if incs:
        # Property 14: one opening-announcement per opening.
        assert len(d.lines) == incs[0].opening
        assert sum(1 for line in d.lines if line.startswith("REOPENED")) == incs[0].opening - 1
    else:
        assert d.lines == []


@given(st.integers(0, 600), ops)
def test_reopen_rules_without_a_store(reopen_after, sequence):
    check(reopen_after, sequence, False)


@given(st.integers(0, 600), ops)
def test_reopen_rules_with_a_store(reopen_after, sequence):
    check(reopen_after, sequence, True)
