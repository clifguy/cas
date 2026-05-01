"""Backfill the seven theology-vault Editorial Guidelines abstracts.

Reads the new abstracts from ab_editorial_guidelines_report.md (produced
by ab_editorial_guidelines.py), then UPDATEs the corresponding rows in
the theology vault's graph store. Mirrors the field updates that
sage_reabstract performs:

  semantic_abstract = <new>
  pipeline_status   = 'abstraction_complete'
  updated_at        = <now, UTC ISO 8601>

Does not regenerate abstracts; relies entirely on the report. Safe to
run while the SAGE MCP server is alive (SQLite file locks serialize
writes; SAGE reads documents per-request so cached state is not a
concern).
"""

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "ab_editorial_guidelines_report.md"
GRAPH_DB = Path.home() / "sage_vaults" / "theology" / "brain" / "graph.db"


def parse_report(text: str) -> list[tuple[str, str]]:
    """Return list of (doc_id, new_abstract) tuples from the report.

    The report's per-document section has the shape:

        ## <Label>
        - **Title:** ...
        - **doc_type:** `...`
        - **Document id:** `<doc_id>`
        ...
        ### OLD (stored) abstract
        > <old>
        ### NEW abstract (descriptive, doc_type-aware)
        > <new line 1>
        > <new line 2>
        ---

    The "> "-prefixed lines following the NEW header constitute the
    abstract; an empty line, the next "---" separator, or EOF
    terminate it.
    """
    sections = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    out: list[tuple[str, str]] = []
    for section in sections:
        m_id = re.search(
            r"\*\*Document id:\*\*\s+`([^`]+)`", section
        )
        if not m_id:
            continue
        doc_id = m_id.group(1)

        m_new = re.search(
            r"### NEW abstract[^\n]*\n+((?:>.*\n?)+)",
            section,
        )
        if not m_new:
            continue
        block = m_new.group(1)
        lines = []
        for raw_line in block.splitlines():
            stripped = raw_line.lstrip()
            if not stripped.startswith(">"):
                break
            # Remove leading '> ' or '>'
            content = stripped[1:].lstrip()
            lines.append(content)
        # Re-join. Multi-line abstracts are very rare here (the
        # descriptive abstracts are single paragraphs), but be safe.
        abstract = " ".join(line for line in lines if line).strip()
        if abstract:
            out.append((doc_id, abstract))
    return out


def main() -> int:
    if not REPORT_PATH.exists():
        print(f"ERROR: report not found at {REPORT_PATH}", file=sys.stderr)
        return 1
    if not GRAPH_DB.exists():
        print(f"ERROR: graph.db not found at {GRAPH_DB}", file=sys.stderr)
        return 1

    pairs = parse_report(REPORT_PATH.read_text())
    if not pairs:
        print("ERROR: no (doc_id, abstract) pairs parsed", file=sys.stderr)
        return 1

    print(f"Parsed {len(pairs)} new abstracts from {REPORT_PATH.name}\n")
    for doc_id, abstract in pairs:
        preview = abstract[:80].replace("\n", " ")
        print(f"  {doc_id}: {preview}...")
    print()

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(GRAPH_DB)
    try:
        # Verify all seven exist before writing any
        cur = conn.execute(
            "SELECT id, length(semantic_abstract) FROM documents "
            f"WHERE id IN ({','.join('?' * len(pairs))})",
            [p[0] for p in pairs],
        )
        existing = {row[0]: row[1] for row in cur.fetchall()}
        missing = [doc_id for doc_id, _ in pairs if doc_id not in existing]
        if missing:
            print(f"ERROR: missing rows: {missing}", file=sys.stderr)
            return 2

        # Update under a single transaction
        with conn:
            for doc_id, abstract in pairs:
                conn.execute(
                    "UPDATE documents SET semantic_abstract = ?, "
                    "pipeline_status = 'abstraction_complete', "
                    "updated_at = ? WHERE id = ?",
                    (abstract, now_iso, doc_id),
                )

        # Verify
        cur = conn.execute(
            "SELECT id, length(semantic_abstract), pipeline_status, "
            "updated_at FROM documents "
            f"WHERE id IN ({','.join('?' * len(pairs))})",
            [p[0] for p in pairs],
        )
        print("Post-update verification:")
        for row in cur.fetchall():
            print(
                f"  {row[0]}: len={row[1]:4d}  status={row[2]}  "
                f"updated_at={row[3]}"
            )
    finally:
        conn.close()

    print(f"\nBackfilled {len(pairs)} abstracts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
