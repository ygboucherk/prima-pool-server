"""Server CLI: run the control plane with uvicorn."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load `.env` from the current directory (and parents) if present.

    The compose path already passes env vars through (see docker-compose.yml),
    but running `prima-pool-server` directly relies on `.env` being loaded into
    the process. python-dotenv is a declared dependency; if it's somehow
    missing we degrade gracefully (the operator can export vars manually).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return
    # Look for `.env` in the CWD and each parent (mimics `docker compose`).
    for path in [Path.cwd(), *Path.cwd().parents]:
        candidate = path / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            return


def main() -> None:
    parser = argparse.ArgumentParser(prog="prima-pool-server", description="prima-pool control plane")
    parser.add_argument("--host", default=None, help="Bind host (default: PRIMA_POOL_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: PRIMA_POOL_PORT or 8000)")
    parser.add_argument("--store", default=None, help="SQLite DB path (default: PRIMA_POOL_STORE_PATH)")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    _load_dotenv()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    host = args.host or os.environ.get("PRIMA_POOL_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("PRIMA_POOL_PORT", "8000"))
    if args.store:
        os.environ["PRIMA_POOL_STORE_PATH"] = args.store

    import uvicorn

    uvicorn.run(
        "prima_pool_server.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
