#!/usr/bin/env python3
"""One-off repair: remove every trace of a specific document from a SAGE vault.

SAGE has no public document-deletion API by design (documents are versioned,
not deleted; supersede edges carry the history). This script exists for
content-integrity repair: when a document record is unrecoverably out of
sync with its source bytes (file replaced on disk, corruption, accidental
ingest), the only fix is to remove the orphaned record so a clean
re-ingestion can produce a fresh record with the right hash and projection.

What gets removed
-----------------
1. SQLite (graph.db):
   - ``document_tags`` rows for this document_id
   - ``edges`` rows where source_id OR target_id is this document
   - ``documents`` row
2. LanceDB (chunks table):
   - All chunks where document_id = this document
3. Source file on disk (vault-local copy):
   - Removed by default; pass --keep-source to skip

What gets preserved
-------------------
- Outgoing/incoming edges from this doc are dumped to a JSON sidecar before
  deletion, so the caller can re-create them via ``create_edge`` after the
  cleaned-up document is re-ingested. The sidecar path is printed at the
  end of the dry run and again before deletion proceeds.

What does NOT get touched
-------------------------
- Documents on the other side of this doc's edges. Only this doc and its
  direct edges are affected.
- Symlink views under ``$brain_root/sources/views/``. After running this
  script, call ``mcp__sage__sage_refresh_views`` to drop and rebuild
  them, or wait for the next ingest to do so.

Usage
-----
    .venv/bin/python -m scripts.repair_delete_document VAULT_ID DOC_ID

    # Apply the deletion (default is dry-run):
    .venv/bin/python -m scripts.repair_delete_document VAULT_ID DOC_ID --execute

    # Keep the source file in place (e.g., for forensic inspection):
    .venv/bin/python -m scripts.repair_delete_document VAULT_ID DOC_ID --execute --keep-source

The SAGE MCP server may hold open handles to the same SQLite/LanceDB.
SQLite WAL handles concurrent writers; LanceDB ``delete()`` from a fresh
``lancedb.connect()`` handle coexists with the running server's handle.
No server restart required.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import lancedb

from sage.config import load_vault_config
from sage.vault_management import config_path_for_vault


def _dump_edges_sidecar(brain_root: Path, doc_id: str, rows: list[dict]) -> Path:
    """Write outgoing/incoming edge details to a JSON sidecar under
    ``$brain_root/repair_logs/`` so the rationale can be re-applied
    after re-ingestion.
    """
    out_dir = brain_root / "repair_logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{doc_id}_edges_{ts}.json"
    path.write_text(json.dumps(rows, indent=2, default=str))
    return path


def repair(vault_id: str, doc_id: str, *, execute: bool, keep_source: bool) -> int:
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser()
    sources_root = Path(config.vault.storage_root).expanduser()
    sqlite_path = brain_root / "graph.db"
    lancedb_dir = brain_root / "lancedb"

    if not sqlite_path.exists():
        print(f"graph.db not found: {sqlite_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Confirm document exists; pull source_path so we can locate the file.
    doc_row = c.execute(
        "SELECT id, title, source_path, lifecycle_status FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    if doc_row is None:
        print(f"document not found in vault {vault_id!r}: {doc_id}", file=sys.stderr)
        conn.close()
        return 2

    # Count affected rows in each table.
    tag_count = c.execute(
        "SELECT COUNT(*) FROM document_tags WHERE document_id = ?", (doc_id,)
    ).fetchone()[0]

    out_edges = [
        dict(r) for r in c.execute("SELECT * FROM edges WHERE source_id = ?", (doc_id,)).fetchall()
    ]
    in_edges = [
        dict(r) for r in c.execute("SELECT * FROM edges WHERE target_id = ?", (doc_id,)).fetchall()
    ]

    # Count chunks in LanceDB.
    chunk_count = 0
    if lancedb_dir.exists():
        db = lancedb.connect(str(lancedb_dir))
        if "chunks" in db.table_names():
            tbl = db.open_table("chunks")
            chunk_count = tbl.count_rows(filter=f"document_id = '{doc_id}'")

    # Resolve source-file path relative to storage_root.
    src_rel = doc_row["source_path"]
    src_abs = (sources_root / src_rel).resolve()
    src_exists = src_abs.exists()

    print(f"Vault:        {vault_id}")
    print(f"Document:     {doc_id}")
    print(f"  title:           {doc_row['title']}")
    print(f"  source_path:     {src_rel}")
    print(f"  lifecycle:       {doc_row['lifecycle_status']}")
    print()
    print("Will delete:")
    print(f"  document_tags rows:    {tag_count}")
    print(f"  edges (outgoing):      {len(out_edges)}")
    print(f"  edges (incoming):      {len(in_edges)}")
    print("  documents row:         1")
    print(f"  lancedb chunks:        {chunk_count}")
    if keep_source:
        print(f"  source file:           preserved ({src_abs})")
    else:
        print(
            f"  source file:           {'(will delete) ' if src_exists else '(absent) '}{src_abs}"
        )
    print()

    if not (out_edges or in_edges):
        print("(no edges to preserve)")
    else:
        print("Edges to be preserved in sidecar:")
        for e in out_edges + in_edges:
            direction = "OUT ->" if e["source_id"] == doc_id else "<- IN"
            other = e["target_id"] if direction == "OUT ->" else e["source_id"]
            print(f"  {direction} {other}  ({e['edge_type']})")
        print()

    if not execute:
        print("(dry-run; pass --execute to apply)")
        conn.close()
        return 0

    print("Applying repair...")

    # Dump edges to sidecar BEFORE deletion.
    sidecar = None
    if out_edges or in_edges:
        sidecar = _dump_edges_sidecar(
            brain_root,
            doc_id,
            [{"direction": "out", **e} for e in out_edges]
            + [{"direction": "in", **e} for e in in_edges],
        )
        print(f"  edges -> {sidecar}")

    # SQLite deletions in dependency order: children before parent.
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
        c.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (doc_id, doc_id),
        )
        c.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
        print("  sqlite: tags + edges + document row deleted")
    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()

    # LanceDB delete.
    if chunk_count > 0:
        db = lancedb.connect(str(lancedb_dir))
        tbl = db.open_table("chunks")
        tbl.delete(f"document_id = '{doc_id}'")
        print(f"  lancedb: {chunk_count} chunk(s) deleted")

    # Source file delete (or move to a quarantine location).
    if not keep_source and src_exists:
        # Move aside rather than rm -- preserves recoverability if this turns
        # out to be the wrong file.
        quarantine_dir = brain_root / "repair_logs" / "quarantined_sources"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = quarantine_dir / f"{doc_id}_{ts}_{src_abs.name}"
        src_abs.rename(dest)
        print(f"  source: moved to {dest}")

    print()
    print("Done. Next steps:")
    print(f"  1. Refresh symlink views: mcp__sage__sage_refresh_views(vault_id={vault_id!r})")
    print("  2. Re-ingest the corrected source via mcp__sage__sage_ingest.")
    if sidecar:
        print(f"  3. Re-create edges using the sidecar at {sidecar}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove every trace of a specific document from a SAGE vault "
            "(content-integrity repair only; see script docstring)."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. example_vault)")
    parser.add_argument("document_id", help="Document id to remove")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the deletion. Without this flag, prints a plan and exits.",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Do not move the on-disk source file aside.",
    )
    args = parser.parse_args()

    rc = repair(
        args.vault_id,
        args.document_id,
        execute=args.execute,
        keep_source=args.keep_source,
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
