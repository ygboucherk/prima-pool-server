"""Server CLI: run the control plane with uvicorn."""
from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="prima-pool-server", description="prima-pool control plane")
    parser.add_argument("--host", default=None, help="Bind host (default: PRIMA_POOL_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: PRIMA_POOL_PORT or 8000)")
    parser.add_argument("--store", default=None, help="JSON store path (default: PRIMA_POOL_STORE_PATH)")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

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
