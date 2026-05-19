#!/usr/bin/env python3
"""Compact a vault's LanceDB chunks table and prune stale FTS index versions.

Post-hoc cleanup for cases where a bulk chunk rewrite (e.g. reabstracting
every document, or rebuilding synthetic headers) leaves the
``chunks.lance/_indices/`` directory full of retained FTS index versions
that LanceDB will not drop without an explicit ``optimize`` call.

Opens a fresh ``lancedb.connect`` handle (so it is not blocked by version
pinning from a long-running SAGE MCP server handle) and runs
``table.optimize(cleanup_older_than=timedelta(0))`` on the ``chunks``
table. Mirrors the compaction step in ``scripts/rebuild_synthetic_headers.py``.

Usage::

    .venv/bin/python -m scripts.compact_lancedb VAULT_ID
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sage.config import load_vault_config
from sage.vault_management import config_path_for_vault


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("vault_id")
    args = parser.parse_args()

    config_path = config_path_for_vault(args.vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser()
    lancedb_dir = brain_root / "lancedb"
    chunks_path = lancedb_dir / "chunks.lance"

    if not chunks_path.exists():
        print(f"chunks table not found: {chunks_path}", file=sys.stderr)
        return 2

    before = _dir_size(chunks_path)
    indices_before = _dir_size(chunks_path / "_indices")
    print(f"vault: {args.vault_id}")
    print(f"chunks.lance: {_human(before)} (of which _indices/: {_human(indices_before)})")

    import lancedb

    print("\nCompacting and pruning old versions...")
    started = datetime.now(timezone.utc)
    db = lancedb.connect(str(lancedb_dir))
    table = db.open_table("chunks")
    table.optimize(cleanup_older_than=timedelta(0))
    elapsed = datetime.now(timezone.utc) - started
    print(f"Done in {elapsed.total_seconds():.1f}s.")

    after = _dir_size(chunks_path)
    indices_after = _dir_size(chunks_path / "_indices")
    freed = before - after
    print(f"\nchunks.lance: {_human(after)} (of which _indices/: {_human(indices_after)})")
    print(f"freed: {_human(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
