"""Run the mesh: ``python -m chimericmesh``. Rotate the admin token: ``python -m chimericmesh token --rotate``."""
import logging
import os
import sys
from pathlib import Path

import uvicorn

from . import fsperms
from .api import create_app
from .services import AccessConfigError
from .services.token import TokenStore, resolve_token_path
from .store import StoreCorruptError, StoreOpenError
from .store.config import describe_startup, resolve_home

USAGE = "usage: python -m chimericmesh [token --rotate]"
GRACEFUL_SHUTDOWN_SECONDS = 3     # uvicorn cancels a request that is still held after this long (R1-M2)


class MeshServer(uvicorn.Server):
    """A server whose exit request first wakes every held `wait_for_work` call.

    Uvicorn waits for in-flight requests before it runs the lifespan teardown, so a held long poll would keep
    the hub open for up to its full wait. `aclose` makes each waiter return `timeout` at once.
    """

    def handle_exit(self, sig: int, frame) -> None:
        agent = getattr(self.config.app.state, "agent", None)
        if agent is not None:
            agent.aclose()
        super().handle_exit(sig, frame)


def build_server(app, host: str, port: int) -> uvicorn.Server:
    """The server `main` runs. A helper so a test can inspect the configuration."""
    config = uvicorn.Config(app, host=host, port=port, log_level="info",
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
    return MeshServer(config)


def rotate_token() -> int:
    """Replace the token file and print its path, never the token. A running hub keeps its old token until restart."""
    path = resolve_token_path(os.environ, Path(resolve_home()))
    try:
        if not path.parent.is_dir():
            os.makedirs(path.parent, exist_ok=True)
            fsperms.secure_dir(path.parent)
        # The old content is not read: replacing an invalid file is exactly what --rotate is for.
        TokenStore(path, "").rotate()
    except OSError as exc:
        print(f"token: could not replace {path}: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(path)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["token"]:
        if args[1:] != ["--rotate"]:
            print(USAGE, file=sys.stderr)
            sys.exit(2)
        sys.exit(rotate_token())
    try:
        app = create_app()
    except StoreCorruptError as exc:
        print(f"store: {exc.path} is not a valid SQLite database; it was left untouched", file=sys.stderr)
        sys.exit(2)
    except StoreOpenError as exc:
        print(f"store: {exc}", file=sys.stderr)
        sys.exit(2)
    except AccessConfigError as exc:
        print(f"access: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.getLogger("chimericmesh").warning(describe_startup(app.state.store.path))
    token_path = app.state.access.token_path
    if token_path is not None:
        print(f"admin token file: {token_path}", flush=True)
    build_server(app, os.environ.get("CHIMERICMESH_BIND", "127.0.0.1"),
                 int(os.environ.get("CHIMERICMESH_HTTP_PORT", 8000))).run()


if __name__ == "__main__":
    main()
