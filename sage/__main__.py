"""SAGE server entry point: python -m sage [--vault-root PATH]"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from sage.app import create_app


def _resolve_vault_root(
    args: argparse.Namespace, env: dict[str, str] | None = None
) -> Path:
    """Resolve the vault root directory.

    Resolution order: ``args.vault_root`` (from the ``--vault-root`` flag) →
    ``SAGE_VAULT_ROOT`` env var → ``~/sage_vaults`` default. Tilde-expansion
    is applied; the path is not required to exist (discovery treats a
    missing root as an empty vault set).

    Args:
        args: Parsed args namespace, expected to have a ``vault_root``
            attribute (``None`` if the flag was not given).
        env: Environment mapping. Defaults to ``os.environ``.

    Returns:
        Resolved absolute path to the vault root.
    """
    if env is None:
        env = os.environ

    flag_value = getattr(args, "vault_root", None)
    if flag_value is not None:
        return Path(flag_value).expanduser()

    env_value = env.get("SAGE_VAULT_ROOT")
    if env_value:
        return Path(env_value).expanduser()

    return Path.home() / "sage_vaults"

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
        description=(
            "Start the SAGE Core API server. Vaults are auto-discovered "
            "from the vault root (every directory containing vault_config.yaml). "
            "To advance schemas, run python -m sage.migrate separately."
        ),
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help=(
            "Directory containing one subdirectory per vault. "
            "Defaults to $SAGE_VAULT_ROOT, then ~/sage_vaults."
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
    vault_root = _resolve_vault_root(args)

    app = create_app(vault_root=vault_root)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
