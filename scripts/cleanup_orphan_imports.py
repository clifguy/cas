#!/usr/bin/env python3
"""Remove orphaned files from a SAGE vault's imports/ directory.

Compares files on disk against source_path values in the vault's graph
database. Files not referenced by any document record are orphans,
typically created by the duplicate-on-reimport bug in _ensure_vault_local.

Usage:
    python scripts/cleanup_orphan_imports.py                  # dry-run
    python scripts/cleanup_orphan_imports.py --execute        # delete orphans
    python scripts/cleanup_orphan_imports.py --vault OTHER_ID # different vault
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml


def load_vault_config(vault_id: str) -> dict:
    config_path = Path.home() / "sage_vaults" / vault_id / "vault_config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Vault config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_db_source_paths(brain_root: Path) -> set[str]:
    db_path = brain_root / "graph.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Graph database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT source_path FROM documents").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def find_orphans(storage_root: Path, db_paths: set[str]) -> list[Path]:
    imports_dir = storage_root / "imports"
    if not imports_dir.exists():
        return []

    orphans = []
    for f in sorted(imports_dir.iterdir()):
        if f.is_file():
            vault_relative = f"imports/{f.name}"
            if vault_relative not in db_paths:
                orphans.append(f)
    return orphans


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove orphaned vault imports")
    parser.add_argument("--vault", default="pim_health", help="Vault ID (default: pim_health)")
    parser.add_argument(
        "--execute", action="store_true", help="Actually delete orphans (default: dry-run)"
    )
    args = parser.parse_args()

    config = load_vault_config(args.vault)
    storage_root = Path(config["vault"]["storage_root"]).expanduser().resolve()
    brain_root = Path(config["vault"]["brain_root"]).expanduser().resolve()

    print(f"Vault:        {args.vault}")
    print(f"Storage root: {storage_root}")
    print(f"Brain root:   {brain_root}")
    print()

    db_paths = get_db_source_paths(brain_root)
    print(f"Documents in database: {len(db_paths)}")

    imports_dir = storage_root / "imports"
    if imports_dir.exists():
        all_files = [f for f in imports_dir.iterdir() if f.is_file()]
        print(f"Files in imports/:     {len(all_files)}")
    else:
        print("No imports/ directory found.")
        return

    orphans = find_orphans(storage_root, db_paths)
    print(f"Orphaned files:        {len(orphans)}")
    print()

    if not orphans:
        print("No orphans to clean up.")
        return

    for f in orphans:
        print(f"  {'DELETE' if args.execute else 'ORPHAN'}: {f.name}")

    if args.execute:
        for f in orphans:
            f.unlink()
        print(f"\nDeleted {len(orphans)} orphaned files.")
        remaining = [f for f in imports_dir.iterdir() if f.is_file()]
        print(f"Files remaining in imports/: {len(remaining)}")
    else:
        print(f"\nDry run. Use --execute to delete {len(orphans)} orphaned files.")


if __name__ == "__main__":
    main()
