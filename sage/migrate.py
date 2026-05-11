"""SAGE schema-migration CLI.

Run separately from the serving binary so the operational steps are
distinct: ``python -m sage`` serves discovered vaults; ``python -m
sage.migrate`` advances schemas. The serving binary refuses to start
when a vault's schema is out of date and directs the operator here.

Usage:
    python -m sage.migrate                       # migrate every discovered vault
    python -m sage.migrate --vault VAULT_ID      # migrate one vault
    python -m sage.migrate --vault A --vault B   # migrate selected vaults
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from sage.config import VaultConfig, load_vault_config
from sage.mcp_init import initialize_services
from sage.vault_discovery import discover_vault_configs

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the migrate CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m sage.migrate",
        description=(
            "Apply pending schema migrations to discovered SAGE vaults. "
            "Run before starting the server when storage schemas advance."
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
        "--vault",
        action="append",
        default=None,
        metavar="VAULT_ID",
        help=(
            "Restrict migration to the named vault. Repeatable. "
            "If omitted, every discovered vault is migrated."
        ),
    )
    return parser


def _resolve_vault_root(args: argparse.Namespace, env: dict[str, str] | None = None) -> Path:
    """Resolve the vault root: --vault-root → SAGE_VAULT_ROOT → ~/sage_vaults."""
    if env is None:
        env = os.environ
    if args.vault_root is not None:
        return Path(args.vault_root).expanduser()
    env_value = env.get("SAGE_VAULT_ROOT")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / "sage_vaults"


async def _migrate_vault(config: VaultConfig, config_path: Path) -> None:
    """Apply pending schema migrations to one vault, then clean up.

    Initializes the graph and content stores with ``migrate=True`` and
    closes them. Raises on any underlying storage error so the caller
    can decide whether to halt or continue.
    """
    services = await initialize_services(config, migrate=True, config_path=config_path)
    await services.graph_store.close()


async def _run(args: argparse.Namespace) -> int:
    """Async body of main(). Returns process exit code."""
    vault_root = _resolve_vault_root(args)
    discovered = discover_vault_configs(vault_root)

    if not discovered:
        logger.info("No vaults found under %s; nothing to migrate.", vault_root)
        return 0

    # Build (vault_id, config, config_path) triples in discovery order.
    candidates: list[tuple[str, VaultConfig, Path]] = []
    for cp in discovered:
        try:
            cfg = load_vault_config(cp)
        except Exception as exc:
            logger.error("Failed to read %s: %s", cp, exc)
            return 1
        candidates.append((cfg.vault.id, cfg, cp))

    requested: set[str] | None = set(args.vault) if args.vault is not None else None
    if requested is not None:
        available = {vid for vid, _, _ in candidates}
        unknown = requested - available
        if unknown:
            logger.error(
                "Unknown vault id(s): %s. Available: %s",
                ", ".join(sorted(unknown)),
                ", ".join(sorted(available)) or "(none)",
            )
            return 2
        candidates = [c for c in candidates if c[0] in requested]

    for vault_id, cfg, cp in candidates:
        logger.info("Migrating vault %s ...", vault_id)
        try:
            await _migrate_vault(cfg, cp)
        except Exception as exc:
            logger.error(
                "Migration failed for vault %s at %s: %s. Halting; remaining vaults not attempted.",
                vault_id,
                cp,
                exc,
            )
            return 1
        logger.info("Vault %s migrated.", vault_id)

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code (0 success, non-zero failure)."""
    args = _build_parser().parse_args(argv)
    # Ensure log output is visible to operators running this as a script.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    import sys

    sys.exit(main())
