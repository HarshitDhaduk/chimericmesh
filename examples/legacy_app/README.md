# Sample legacy service

A small stdlib-only service that ChimericMesh can watch for real. A proxy worker health-checks an upstream over TCP and reports its log lines to the hub as RFC 3164 syslog datagrams. The upstream is a local "blackhole" that accepts connections and never answers, so every check times out.

**The bug in `upstream.py` is intentional.** It exists so the `mesh-responder` agent has a real incident to fix. Do not fix it by hand before the demo.

## Run

Start the hub first (`python -m chimericmesh`), then:

```
python examples/legacy_app/run.py
```

Options: `--hub host:port` (default `127.0.0.1:5514`, or env `LEGACY_APP_SYSLOG`), `--no-blackhole` (use an upstream you run yourself), `--upstream-port`, `--duration seconds`. Host name comes from `LEGACY_APP_HOST` (default: this machine's name). Stop with Ctrl+C.

## What the hub shows

Within a few seconds the dashboard at http://127.0.0.1:8000 opens a `network` incident for "upstream timed out connecting to 127.0.0.1:19099", with a code hint pointing at `examples/legacy_app/upstream.py`. The event count climbs steadily.

## Tests

```
python -m pytest examples/legacy_app/tests -q
```

These describe how the client must behave against a dead upstream. They fail on the shipped code and should pass after a correct fix. The main suite (`python -m pytest -q`) passes in both states.

## Reset after an agent fixed it

```
git checkout -- examples/legacy_app
git clean -fd examples/legacy_app
```
