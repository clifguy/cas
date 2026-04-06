"""SAGE server entry point: python -m sage <config_path>"""

import sys
from pathlib import Path

import uvicorn

from sage.app import create_app


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m sage <config.yaml>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    app = create_app(config_path=config_path)
    uvicorn.run(app, host="127.0.0.1", port=8000)


main()
