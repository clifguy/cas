"""Vault-scoped maintenance/admin operations (CAS-ADR-029).

Pilot operation: schema migration for a single vault in the running
session. Subsequent ``sage_admin_*`` operations slot into the same
three-layer service + router + MCP-tool shape.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from sage.config import VaultConfig
from sage.models.schemas import MigrationReport, MigrationReportEntry
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    BACKFILL_PLAN,
    MIGRATION_PLAN,
    TABLES,
    Backfill,
    Migration,
    pending_backfills,
    pending_migrations,
)

if TYPE_CHECKING:
    from sage.services.vault_registry import VaultRegistryService


def _detect_pending_work(db_path: Path) -> tuple[list[Migration], list[Backfill]]:
    """Detect what GraphStore.initialize(migrate=True) would apply.

    Returns ``(pending_alters, pending_bfs)`` as the graph_store would
    see them after applying the pending ALTER TABLE migrations: backfill
    detection runs against a temp copy of the db whose schema has the
    post-migration columns. This mirrors the re-detect step in
    ``GraphStore._initialize_sync`` so backfills that depend on newly-
    added columns (e.g., the T-0080 rationale_kind backfill, whose
    detector queries the ``rationale_kind`` column that the migration
    itself adds) are surfaced rather than silently skipped.

    The live db file is not touched; the temp copy is discarded.
    """
    live = sqlite3.connect(str(db_path))
    try:
        pending_alters = pending_migrations(live, MIGRATION_PLAN)
    finally:
        live.close()

    with tempfile.NamedTemporaryFile(
        suffix=".db", delete=False, prefix="sage_migrate_detect_"
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(db_path, tmp_path)
        sim = sqlite3.connect(str(tmp_path))
        try:
            # Mirror GraphStore._initialize_sync ordering: CREATE TABLE
            # IF NOT EXISTS for every table (so a backfill detector that
            # queries a not-yet-extant table like ``document_tags`` does
            # not raise OperationalError and get silently swallowed),
            # then apply pending ALTER TABLE migrations so backfills
            # whose detectors query newly-added columns see them.
            for ddl in TABLES:
                sim.execute(ddl)
            for m in pending_alters:
                sim.execute(m.ddl)
            pending_bfs = pending_backfills(sim, BACKFILL_PLAN)
        finally:
            sim.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    return pending_alters, pending_bfs


class MaintenanceService:
    """Pilot of the maintenance/admin API surface (CAS-ADR-029)."""

    def __init__(
        self,
        vault_id: str,
        db_path: Path,
        graph_store: GraphStore,
        config: VaultConfig,
        registry_service: "VaultRegistryService",
    ) -> None:
        self._vault_id = vault_id
        self._db_path = db_path
        self._graph_store = graph_store
        self._config = config
        self._registry_service = registry_service

    async def migrate_vault(self) -> MigrationReport:
        """Apply pending schema migrations and reload the vault in-session.

        Read-only pre-detect: enumerate which MIGRATION_PLAN columns and
        BACKFILL_PLAN entries are pending, simulating the post-migration
        schema on a temp copy so backfills that depend on newly-added
        columns are surfaced. If nothing is pending, return an empty
        report without touching the running graph_store -- the
        idempotent no-op path.

        Otherwise: close the running graph_store, open a fresh one
        solely for migration, run ``initialize(migrate=True)``, close
        it, then ask the VaultRegistryService to reload the vault so the
        registry holds a freshly-initialized SAGEServices bundle whose
        graph_store carries the post-migration schema.
        """
        pending_alters, pending_bfs = _detect_pending_work(self._db_path)

        columns_added = [
            MigrationReportEntry(table=m.table, column=m.column) for m in pending_alters
        ]
        backfills_applied = [b.name for b in pending_bfs]

        if not columns_added and not backfills_applied:
            return MigrationReport(
                vault_id=self._vault_id,
                columns_added=[],
                backfills_applied=[],
            )

        await self._graph_store.close()
        fresh = GraphStore(self._db_path)
        await fresh.initialize(migrate=True)
        await fresh.close()

        await self._registry_service.reload(self._vault_id, self._config)

        return MigrationReport(
            vault_id=self._vault_id,
            columns_added=columns_added,
            backfills_applied=backfills_applied,
        )
