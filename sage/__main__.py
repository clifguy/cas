"""SAGE server entry point: python -m sage <config.yaml> [...] [--migrate]"""

import argparse
import sys
from pathlib import Path

import uvicorn

from sage.app import create_app

# Uvicorn's default log_config with a timestamp prefix added, matching the
# `[MM/DD/YY HH:MM:SS]` style RichHandler installs for the rest of the
# process. `propagate=False` keeps these records out of the root logger so
# the timestamp does not get re-applied by Rich.
UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "[%(asctime)s] %(levelprefix)s %(message)s",
            "datefmt": "%m/%d/%y %H:%M:%S",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%m/%d/%y %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {
            "handlers": ["access"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sage",
        description="Start the SAGE Core API server for one or more vaults.",
    )
    parser.add_argument(
        "config_paths",
        nargs="+",
        metavar="config.yaml",
        help="Path to a vault YAML config file. Repeat for multi-vault.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        default=False,
        help=(
            "Apply pending schema migrations to graph and content stores "
            "on startup. Off by default; without this flag, a legacy "
            "schema causes the server to refuse to start."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Uvicorn bind host (default: 127.0.0.1)."
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Uvicorn port (default: 8000)."
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    config_paths: list[Path] = []
    for arg in args.config_paths:
        p = Path(arg)
        if not p.exists():
            print(f"Config file not found: {p}")
            sys.exit(1)
        config_paths.append(p)

    app = create_app(config_paths=config_paths, migrate=args.migrate)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
