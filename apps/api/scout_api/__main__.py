"""Run the API with uvicorn: ``python -m scout_api``.

Provided so the process has the same ``python -m`` entry point as the orchestrator and the
worker, which makes the Compose services and the runbook consistent.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and serve the application."""
    parser = argparse.ArgumentParser(prog="scout_api", description="Scout control-plane API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload", action="store_true", help="reload on source changes (development only)"
    )
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "scout_api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # structlog already owns logging configuration
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
