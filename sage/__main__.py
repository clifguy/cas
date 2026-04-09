"""SAGE server entry point: python -m sage <config_path> [config_path2 ...]"""

import sys
from pathlib import Path

import uvicorn

from sage.app import create_app


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m sage <config.yaml> [config2.yaml ...]")
        sys.exit(1)

    config_paths = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"Config file not found: {p}")
            sys.exit(1)
        config_paths.append(p)

    app = create_app(config_paths=config_paths)
    uvicorn.run(app, host="127.0.0.1", port=8000)


main()
