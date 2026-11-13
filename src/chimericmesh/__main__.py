"""Run the mesh: ``python -m chimericmesh``."""

import os

import uvicorn

from .api import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host=os.environ.get("CHIMERICMESH_BIND", "127.0.0.1"),
        port=int(os.environ.get("CHIMERICMESH_HTTP_PORT", 8000)),
        log_level="info",
    )
