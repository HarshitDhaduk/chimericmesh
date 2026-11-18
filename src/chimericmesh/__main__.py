"""Run the mesh: ``python -m chimericmesh``."""
import logging
import os
import sys

import uvicorn

from .api import create_app
from .store import StoreCorruptError, StoreOpenError
from .store.config import describe_startup


def main() -> None:
    try:
        app = create_app()
    except StoreCorruptError as exc:
        print(f"store: {exc.path} is not a valid SQLite database; it was left untouched", file=sys.stderr)
        sys.exit(2)
    except StoreOpenError as exc:
        print(f"store: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.getLogger("chimericmesh").warning(describe_startup(app.state.store.path))
    uvicorn.run(
        app,
        host=os.environ.get("CHIMERICMESH_BIND", "127.0.0.1"),
        port=int(os.environ.get("CHIMERICMESH_HTTP_PORT", 8000)),
        log_level="info",
    )


if __name__ == "__main__":
    main()
