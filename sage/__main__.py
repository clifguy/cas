"""SAGE server entry point: python -m sage [--vault-root PATH]"""

import argparse
import copy
import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock

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

    This function names the ``~/sage_vaults`` default directly, which every
    other root consumer is forbidden to do (see
    :func:`sage.vault_management.default_vault_root`). The exception is
    structural rather than an oversight: this is where the binding is
    established, before it is published for the rest of the process to resolve
    against, so resolving it through the published root would be circular.

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


def _discovery_paths() -> frozenset[str]:
    """Exact metadata locations probed for the supported MCP resources."""
    root = "/.well-known/"
    families = ("oauth-protected-resource", "oauth-authorization-server", "openid-configuration")
    paths = {root + family for family in families}
    for mount in _MCP_MOUNT_PATHS:
        paths.update(root + family + mount for family in families)
        paths.update(mount + root + family for family in (families[0], families[2]))
    return frozenset(paths)


class _DropMcpAccessLogs(_logging.Filter):
    """Keep transport failures; summarize expected unauthenticated discovery.

    Only successful POSTs to canonical MCP paths are routine traffic. An
    authentication posture not supplied by the application defaults to visible
    discovery logs. Query strings never become diagnostic counter keys.
    """

    def __init__(
        self,
        *,
        auth_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._auth_enabled = auth_enabled
        self._clock = clock
        self._paths = _discovery_paths()
        self._counts: dict[str, int] = {}
        self._last_summary: float | None = None
        self._lock = Lock()

    def flush_summary(self) -> None:
        """Emit remaining counts once, including at orderly server shutdown."""
        with self._lock:
            counts, self._counts = self._counts, {}
        if counts:
            _logging.getLogger("sage.discovery").info(
                "Expected discovery 404s (authentication disabled): count=%d paths=%s; "
                "individual probes available at DEBUG",
                sum(counts.values()),
                counts,
            )

    def filter(self, record: _logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, path, _version, status = args
        if not isinstance(path, str) or not isinstance(method, str) or type(status) is not int:
            return True
        path = path.partition("?")[0]
        if method == "POST" and 200 <= status < 300 and path in _MCP_MOUNT_PATHS:
            return False
        if self._auth_enabled or method != "GET" or status != 404 or path not in self._paths:
            return True
        now = self._clock()
        with self._lock:
            self._counts[path] = self._counts.get(path, 0) + 1
            due = self._last_summary is None or now - self._last_summary >= 60
            if due:
                self._last_summary = now
        _logging.getLogger("sage.discovery").debug("Expected discovery GET %s: 404", path)
        if due:
            self.flush_summary()
        return False


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
    access_filter = _DropMcpAccessLogs(auth_enabled=app.state.auth_enabled)
    log_config = copy.deepcopy(UVICORN_LOG_CONFIG)
    log_config["filters"]["drop_mcp_access"] = {"()": lambda: access_filter}
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_config=log_config)
    finally:
        access_filter.flush_summary()


if __name__ == "__main__":
    main()
