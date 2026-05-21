"""Unit tests for MaintenanceService (T-0086, CAS-ADR-029).

Pilot operation of the maintenance/admin surface. Exercises the four
contracts the service must hold:

1. Pending ALTER TABLE migrations are detected, applied, and reflected
   in the returned MigrationReport.
2. The no-pending-work path returns an empty MigrationReport without
   touching the registry (idempotent re-call).
3. The registry entry is swapped for a freshly-initialized SAGEServices
   bundle after migration (so subsequent operations see the new schema).
4. Pending data backfills (BACKFILL_PLAN) are detected and reported
   alongside the column migrations.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.stubs import StubContentStore
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.schemas import MigrationReport
from sage.services.maintenance import MaintenanceService
from sage.services.vault_registry import VaultRegistryService
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import MIGRATION_PLAN
from tests.sage.test_migrate_flag import _build_legacy_db, _minimal_config


async def _bootstrap_post_migration_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, SAGEServices], SAGEServices, VaultRegistryService]:
    """Initialize a vault with a fully-migrated DB.

    Returns the registry, the registered SAGEServices bundle, and the
    registry_service used to construct it. Stub providers throughout so
    the test does not require a Nomic/Qwen3 model on disk.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = _minimal_config(tmp_path)
    registry: dict[str, SAGEServices] = {}
    registry_service = VaultRegistryService(registry, initialize_services)
    services = await initialize_services(
        config,
        migrate=True,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    registry[config.vault.id] = services
    return registry, services, registry_service


async def _swap_in_legacy_db(
    registry: dict[str, SAGEServices],
    services: SAGEServices,
    registry_service: VaultRegistryService,
) -> tuple[Path, MaintenanceService]:
    """Replace the live graph.db with a legacy-shape one and rewire.

    The freshly-initialized graph_store from
    ``_bootstrap_post_migration_vault`` is closed and discarded; its db
    file is replaced with a legacy-shape DB built by ``_build_legacy_db``.
    A new (uninitialized) GraphStore handle is constructed against the
    legacy file and bound to a new MaintenanceService. The registry entry
    is mutated to point at the legacy graph_store + the new
    MaintenanceService so the reload path inside ``migrate_vault`` closes
    the right handle.
    """
    db_path = Path(services.config.vault.brain_root) / "graph.db"
    await services.graph_store.close()
    db_path.unlink()
    _build_legacy_db(db_path)

    legacy_gs = GraphStore(db_path)
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=db_path,
        graph_store=legacy_gs,
        config=services.config,
        registry_service=registry_service,
    )
    registry[services.config.vault.id] = dataclasses.replace(
        services,
        graph_store=legacy_gs,
        maintenance_service=maintenance,
    )
    return db_path, maintenance


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _seed_backfill_triggers(db_path: Path) -> None:
    """Seed two rows that trigger both BACKFILL_PLAN entries.

    1. A document carrying a tags JSON array, with no corresponding
       ``document_tags`` row (the table itself is absent on a legacy
       schema). After migration, the document_tags backfill populates
       the derived join table from this document.
    2. An edges row whose rationale starts with the recognized
       ``[filename_code_match]`` prefix. After the ``rationale_kind``
       column migration applies (with default 'manual'), the
       rationale_kind backfill re-classifies the row.
    """
    now = datetime.now(timezone.utc).isoformat()
    # Literal SQL (not f-string composition) keeps ruff S608 happy. The 11
    # placeholders match the 11 columns; the value tuples are 11-wide.
    insert_doc_sql = (
        "INSERT INTO documents "
        "(id, title, source_type, source_path, tags, source_content_hash, "
        "adapter_version, created_by, created_at, last_modified_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        # Tagged document drives the document_tags backfill.
        conn.execute(
            insert_doc_sql,
            (
                "src_doc",
                "Source",
                "markdown",
                "src/src.md",
                '["alpha", "beta"]',
                "hash_src",
                "0.1.0",
                "tester",
                now,
                "tester",
                now,
            ),
        )
        # Second doc satisfies the edges FK without itself carrying tags.
        conn.execute(
            insert_doc_sql,
            (
                "tgt_doc",
                "Target",
                "markdown",
                "src/tgt.md",
                None,
                "hash_tgt",
                "0.1.0",
                "tester",
                now,
                "tester",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, edge_type, created_at, "
            "notes, rationale) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "seed_edge_1",
                "src_doc",
                "tgt_doc",
                "references",
                now,
                None,
                "[filename_code_match] inferred from shared identifier",
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_migrate_vault_applies_pending_alters_on_legacy_db(tmp_path, monkeypatch):
    """A legacy-shape DB gets every MIGRATION_PLAN column added, and the
    report enumerates the entries that were applied."""
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    db_path, maintenance = await _swap_in_legacy_db(registry, services, registry_service)

    report = await maintenance.migrate_vault()

    assert isinstance(report, MigrationReport)
    assert report.vault_id == services.config.vault.id

    # Every MIGRATION_PLAN column should appear in columns_added.
    expected = {(m.table, m.column) for m in MIGRATION_PLAN}
    actual = {(e.table, e.column) for e in report.columns_added}
    assert actual == expected, f"missing migrations: {expected - actual}"

    # On-disk schema now carries the post-migration columns.
    doc_cols = _columns(db_path, "documents")
    for m in MIGRATION_PLAN:
        if m.table == "documents":
            assert m.column in doc_cols
    edge_cols = _columns(db_path, "edges")
    for m in MIGRATION_PLAN:
        if m.table == "edges":
            assert m.column in edge_cols


async def test_migrate_vault_is_idempotent_on_current_schema(tmp_path, monkeypatch):
    """A re-call against an already-migrated vault returns an empty
    report and does not touch the registry."""
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    # Build a maintenance service bound to the live (post-migration)
    # graph_store, mirroring what initialize_services already produced.
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=services.graph_store,
        config=services.config,
        registry_service=registry_service,
    )
    pre_call_services = registry[services.config.vault.id]

    report = await maintenance.migrate_vault()

    assert report.columns_added == []
    assert report.backfills_applied == []
    # No registry swap on the idempotent path.
    assert registry[services.config.vault.id] is pre_call_services
    # Subsequent reads still work.
    docs = await registry[services.config.vault.id].graph_store.list_all_documents()
    assert docs == []


async def test_migrate_vault_reloads_registry_in_place(tmp_path, monkeypatch):
    """After migration, registry[vault_id] is a *new* SAGEServices
    instance whose graph_store serves reads against the now-migrated
    schema."""
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    _db_path, maintenance = await _swap_in_legacy_db(registry, services, registry_service)
    pre_call_services = registry[services.config.vault.id]

    await maintenance.migrate_vault()

    post_call_services = registry[services.config.vault.id]
    assert post_call_services is not pre_call_services, (
        "registry must hold a freshly-initialized SAGEServices after migration"
    )
    # The post-migration graph_store serves reads (it has been initialized).
    docs = await post_call_services.graph_store.list_all_documents()
    assert docs == []


async def test_migrate_vault_reports_backfills(tmp_path, monkeypatch):
    """Pending BACKFILL_PLAN entries appear in
    MigrationReport.backfills_applied alongside the column migrations."""
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    db_path, maintenance = await _swap_in_legacy_db(registry, services, registry_service)
    _seed_backfill_triggers(db_path)

    report = await maintenance.migrate_vault()

    # Both BACKFILL_PLAN entries activate on a legacy DB:
    # - document_tags: derived join table needs populating
    # - rationale_kind: re-classifies seeded edges with known prefixes
    assert "document_tags" in report.backfills_applied
    assert "rationale_kind" in report.backfills_applied
