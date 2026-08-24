"""SAGE server entry point: python -m sage [--vault-root PATH]"""

import argparse
import os
from pathlib import Path

# Quiet huggingface library noise before any sage import pulls in
# transformers/tokenizers. Env vars are read at library import
# time; setdefault preserves a debugger's explicit override.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "warning")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# httpx and sentence_transformers do not read env vars; raise their
# loggers to WARNING explicitly so model-load HTTP fetches don't flood
# the console. SAGE's own "Loading embedding model" line in
# embedding_nomic.py covers the user-relevant signal.
import logging as _logging  # noqa: E402

for _hf_logger in ("httpx", "sentence_transformers"):
    _logging.getLogger(_hf_logger).setLevel(_logging.WARNING)

# ruff: noqa: E402 -- imports below follow the deliberate pre-import side effects above
import uvicorn

from sage.app import MCP_HTTP_MOUNTS, create_app

#: JSON-RPC endpoint path of every mounted MCP surface, derived from the
#: canonical mount list so a newly mounted surface is suppressed without a
#: second edit here.
_MCP_MOUNT_PATHS: tuple[str, ...] = tuple(path for path, _ in MCP_HTTP_MOUNTS)


def _resolve_vault_root(args: argparse.Namespace, env: dict[str, str] | None = None) -> Path:
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


class _DropMcpAccessLogs(_logging.Filter):
    """Drop uvicorn.access records for any MCP mount's JSON-RPC endpoint.

    Over the Streamable HTTP transport every JSON-RPC message is a ``POST``
    to the mount path itself (e.g. ``/mcp``, ``/mcp_maint``), so each
    logical tool call produces access lines whose URL carries no signal —
    the tool name lives in the request body, and
    ``_LoggingFastMCP.call_tool`` already surfaces it. The suppressed paths
    are derived from the canonical mount list (``_MCP_MOUNT_PATHS``) so a
    newly mounted surface is covered automatically. Matching is
    exact-path-or-query, never bare prefix: the mount path must not shadow
    an unrelated route that merely starts with the same characters, and a
    stray request to a subpath (a 404 worth seeing) stays visible.
    """

    def filter(self, record: _logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = args[2]
        if not isinstance(path, str):
            return True
        return not any(path == m or path.startswith(m + "?") for m in _MCP_MOUNT_PATHS)


# Uvicorn's default log_config with a timestamp prefix added, matching the
# `[MM/DD/YY HH:MM:SS]` style RichHandler installs for the rest of the
# process. `propagate=False` keeps these records out of the root logger so
# the timestamp does not get re-applied by Rich.
UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "drop_mcp_access": {"()": "sage.__main__._DropMcpAccessLogs"},
    },
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "[%(asctime)s] %(levelprefix)s %(message)s",
            "datefmt": "%m/%d/%y %H:%M:%S",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501 -- uvicorn log format string; breaking harms readability
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
            "filters": ["drop_mcp_access"],
            "level": "INFO",
            "propagate": False,
        },
        # Surface ``sage.*`` INFO records (per-tool ``mcp tool: <name>`` line
        # in ``_LoggingFastMCP.call_tool``, embedding/abstraction model-load
        # lines, ingestion progress) through the same formatted handler
        # uvicorn uses. Without this entry, INFO records depend on the
        # RichHandler that ``mcp.server.fastmcp.utilities.logging
        # .configure_logging`` installs on the root logger via
        # ``logging.basicConfig``; that install is a third-party side effect
        # outside SAGE's substrate boundary and can be defeated by any
        # upstream change that reorders or removes it. Pinning the
        # convention here makes successful-call visibility a SAGE-owned
        # contract instead.
        "sage": {"handlers": ["default"], "level": "INFO", "propagate": False},
        # Quiet the MCP SDK's per-request lifecycle chatter over the Streamable
        # HTTP transport. In stateless mode every JSON-RPC request tears down a
        # transient session, logging "Terminating session: None" on
        # ``mcp.server.streamable_http``, and is dispatched through "Processing
        # request of type <X>" on ``mcp.server.lowlevel.server`` — both at INFO,
        # both redundant with the ``mcp tool: <name>`` line
        # ``_LoggingFastMCP.call_tool`` already surfaces. Pinning these two
        # loggers to WARNING drops the INFO records at creation so nothing
        # reaches the root RichHandler; genuine WARNING/ERROR still propagate
        # and stay visible.
        "mcp.server.streamable_http": {"level": "WARNING"},
        "mcp.server.lowlevel.server": {"level": "WARNING"},
    },
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sage",
        description=(
            "Start the SAGE Core API server. Vaults are auto-discovered "
            "from the vault root (every directory containing vault_config.yaml)."
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
    parser.add_argument("--port", type=int, default=8000, help="Uvicorn port (default: 8000).")
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
