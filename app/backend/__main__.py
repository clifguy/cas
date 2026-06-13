"""Standalone CAS application-backend entry point: ``python -m app.backend``.

Starts the backend-for-frontend ASGI app under uvicorn. The app serves the SPA
and reaches SAGE over HTTP; vaults are not loaded here (SAGE runs as its own
process). Mirrors the ``python -m sage`` entry point's flag shape.
"""

from __future__ import annotations

import argparse

import uvicorn

from app.backend.asgi import create_bff_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.backend",
        description=(
            "Start the standalone CAS backend-for-frontend. Serves the SPA "
            "same-origin and reaches SAGE over HTTP via the on-behalf-of "
            "client; SAGE runs as a separate process."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Uvicorn bind host (default: 127.0.0.1; a container passes 0.0.0.0).",
    )
    parser.add_argument("--port", type=int, default=8001, help="Uvicorn port (default: 8001).")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    app = create_bff_app()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
