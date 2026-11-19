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
    uvicorn.run(
        app,
        host=os.environ.get("CHIMERICMESH_BIND", "127.0.0.1"),
        port=int(os.environ.get("CHIMERICMESH_HTTP_PORT", 8000)),
        log_level="info",
    )


if __name__ == "__main__":
    main()
