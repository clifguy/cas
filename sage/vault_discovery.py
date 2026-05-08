"""Vault discovery.

Looks for vault configurations on disk so the SAGE server doesn't need
the operator to enumerate them at launch.

A "vault" is any directory directly under the vault root that contains a
``vault_config.yaml`` file. Hidden directories (names starting with ``.``)
are skipped so macOS metadata directories like ``.DS_Store`` do not
trigger spurious discovery results.
"""

from __future__ import annotations

from pathlib import Path


def discover_vault_configs(root: Path) -> list[Path]:
    """Return paths to ``vault_config.yaml`` files under ``root``.

    Args:
        root: Directory expected to contain one subdirectory per vault.

    Returns:
        Paths to each ``vault_config.yaml`` found, sorted by directory
        name for deterministic ordering. Returns an empty list if
        ``root`` does not exist or contains no qualifying directories.
    """
    if not root.exists() or not root.is_dir():
        return []

    candidates = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        config_path = child / "vault_config.yaml"
        if config_path.is_file():
            candidates.append(config_path)

    return sorted(candidates, key=lambda p: p.parent.name)
