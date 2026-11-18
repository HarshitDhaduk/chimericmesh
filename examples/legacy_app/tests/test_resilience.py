"""Behaviour a correct upstream client must have. Time is simulated, so these run instantly."""
import socket

from upstream import UpstreamClient


class Sim:
    """Fake clock plus a fake network whose upstream can be dead or alive."""

    def __init__(self, alive=lambda t: False):
        self.t = 0.0
        self.alive = alive
        self.attempts: list[tuple[float, bool]] = []  # (start time, succeeded)

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.0)

    def connect(self, address, timeout=None):
        assert len(self.attempts) < 5000, "client is looping without bound"
        ok = self.alive(self.t)
        self.attempts.append((self.t, ok))
        return FakeSocket(self, ok, timeout or 0.2)

    def client(self) -> UpstreamClient:
        return UpstreamClient(connect=self.connect, sleep=self.sleep, clock=self.clock)

    def run(self, until: float) -> None:
        self.client().run_forever(stop=lambda: self.t >= until)

    def starts(self, ok=None) -> list[float]:
        return [t for t, o in self.attempts if ok is None or o == ok]


class FakeSocket:
    def __init__(self, sim, ok, timeout):
        self.sim, self.ok, self.timeout = sim, ok, timeout

    def sendall(self, data):
        pass

    def recv(self, n):
        if self.ok:
            return b"PONG\n"
        self.sim.t += self.timeout
        raise socket.timeout("timed out")

    def close(self):
        pass


def gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


def test_dead_upstream_is_not_hammered():
    sim = Sim()
    sim.run(until=60)
    assert len(sim.attempts) <= 20


def test_delays_between_retries_do_not_shrink_and_grow():
    sim = Sim()
    sim.run(until=300)
    g = gaps(sim.starts())
    assert len(g) >= 5
    assert all(b >= a - 1e-9 for a, b in zip(g, g[1:])), g
    assert g[-1] > g[0] * 2
    assert max(g) <= 120  # capped, so recovery is noticed in reasonable time


def test_client_resumes_after_upstream_recovers():
    sim = Sim(alive=lambda t: t >= 40)
    sim.run(until=40)
    outage_attempts = len(sim.attempts)
    assert outage_attempts <= 20
    sim.run(until=200)
    good = sim.starts(ok=True)
    assert len(good) >= 10
    assert gaps(good)[-1] <= 5  # back to the normal check cadence


def test_success_resets_failure_state():
    served = {"done": False}

    def alive(t):
        if t >= 30 and not served["done"]:
            served["done"] = True
            return True
        return False

    sim = Sim(alive=alive)
    sim.run(until=300)
    times = sim.starts()
    ok_index = next(i for i, (_, o) in enumerate(sim.attempts) if o)
    before = gaps(times[:ok_index])
    after = gaps(times[ok_index + 1:])
    assert before[-1] > before[0] * 2  # delays grew during the first outage
    assert after[0] <= before[0] * 1.5 + 1e-9  # and started over after the success

def test_long_outage_keeps_running_with_bounded_positive_delays():
    failures_wanted = 2500
    now = [0.0]
    sleeps: list[float] = []
    attempts = [0]

    def connect(address, timeout=None):
        attempts[0] += 1
        now[0] += timeout or 0.2
        raise socket.timeout("timed out")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += max(seconds, 0.0)

    client = UpstreamClient(connect=connect, sleep=sleep, clock=lambda: now[0])
    client.run_forever(stop=lambda: attempts[0] >= failures_wanted)
    assert attempts[0] == failures_wanted
    assert sleeps, "client never waited between failed attempts"
    assert all(s > 0 for s in sleeps), min(sleeps)
    cap = max(sleeps)
    assert cap <= 120  # bounded, so recovery is noticed in reasonable time
    assert all(s <= cap + 1e-9 for s in sleeps)
    assert len(sleeps) >= 1000
    assert all(abs(s - cap) < 1e-6 for s in sleeps[-50:]), sleeps[-5:]
