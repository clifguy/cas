"""Read access to a vault's maintenance audit log.

The maintenance audit log (``.maintenance_log.jsonl``) is an append-only
JSONL file in the vault directory; ``MaintenanceService`` writes one line per
maintenance operation. This module owns the log's filename and the read path
the dashboard uses to surface the most recent content-store optimize, keeping
the file contract in one place rather than duplicated across writer and reader.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sage.models.schemas import LastOptimizeSummary

MAINTENANCE_LOG_FILENAME = ".maintenance_log.jsonl"

_OPTIMIZE_OPERATION = "optimize_vault_content_store"


def read_last_optimize_summary(vault_dir: Path) -> LastOptimizeSummary | None:
    """Return a summary of the most recent optimize for a vault, or None.

    Scans the vault's append-only maintenance log and returns the last
    (most recent, by append order) record whose ``operation`` is the
    content-store optimize. Non-optimize and malformed lines are skipped.
    Returns None when the log is absent or holds no optimize record.
    """
    log_path = vault_dir / MAINTENANCE_LOG_FILENAME
    if not log_path.exists():
        return None

    latest: dict | None = None
    for raw in log_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("operation") == _OPTIMIZE_OPERATION:
            latest = record

    if latest is None:
        return None

    return LastOptimizeSummary(
        at=datetime.fromisoformat(latest["timestamp"]),
        bytes_reclaimed=latest["bytes_reclaimed"],
        versions_cleaned=max(0, latest["pre_versions"] - latest["post_versions"]),
        fragments_merged=max(0, latest["pre_fragments"] - latest["post_fragments"]),
    )
