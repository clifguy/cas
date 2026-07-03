"""Unit tests for MaintenanceService (CAS-ADR-029).

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

import contextlib
import dataclasses
import gc
import json
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.enums import EdgeType, PipelineStatus, SourceType, StalenessBasis
from sage.models.schemas import (
    Document,
    DriftReport,
    Edge,
    MigrationReport,
    OptimizeContentStoreReport,
)
from sage.services.maintenance import MaintenanceService
from sage.services.vault_registry import VaultRegistryService
from sage.storage.graph_store import SqliteGraphStore
from sage.storage.migrations import MIGRATION_PLAN
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_migrate_flag import _build_legacy_db, _minimal_config


@contextlib.asynccontextmanager
async def _bootstrap_post_migration_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Async context manager that initializes a vault with a fully-migrated DB.

    Yields ``(registry, services, registry_service)``. On exit the timing
    thread is stopped (via ``initialize_services_for_test``) and the
    original ``services.graph_store`` is closed; if the test body
    swapped the registry slot via ``maintenance.migrate_vault()``,
    callers must close the post-swap bundle explicitly inside the body
    (see ``_close_registry_vault``). Stub providers throughout so
    the test does not require a Nomic/Qwen3 model on disk.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = _minimal_config(tmp_path)
    registry: dict[str, SAGEServices] = {}
    registry_service = VaultRegistryService(registry, initialize_services)
    async with initialize_services_for_test(
        config,
        migrate=True,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        registry[config.vault.id] = services
        yield registry, services, registry_service


async def _close_registry_vault(registry: dict[str, SAGEServices], vault_id: str) -> None:
    """Close the graph_store of whichever SAGEServices currently occupies
    registry[vault_id].

    ``migrate_vault()`` reload paths swap the registry entry for a fresh
    SAGEServices and close the old graph_store as part of the swap; this
    helper closes whatever is bound at teardown time. ``close_timing`` and
    ``SqliteGraphStore.close`` are both idempotent, so this is safe even on the
    no-swap path (where the bootstrap context manager already released them).
    """
    current = registry.get(vault_id)
    if current is not None:
        current.close_timing()
        await current.graph_store.close()


@pytest.fixture
async def post_migration_vault(tmp_path, monkeypatch):
    """Pytest-fixture form of ``_bootstrap_post_migration_vault``.

    Yields ``(registry, services, registry_service)``. On teardown, closes
    the graph_store currently bound to ``registry[vault_id]`` -- which may
    be a *fresh* post-reload SAGEServices if the test triggered
    ``maintenance.migrate_vault()``. Reading the registry at teardown time
    (not capturing ``services`` up front) is what closes the actual leak.
    """
    async with _bootstrap_post_migration_vault(tmp_path, monkeypatch) as (
        registry,
        services,
        registry_service,
    ):
        try:
            yield registry, services, registry_service
        finally:
            await _close_registry_vault(registry, services.config.vault.id)


async def _swap_in_legacy_db(
    registry: dict[str, SAGEServices],
    services: SAGEServices,
    registry_service: VaultRegistryService,
) -> tuple[Path, MaintenanceService]:
    """Replace the live graph.db with a legacy-shape one and rewire.

    The freshly-initialized graph_store from
    ``_bootstrap_post_migration_vault`` is closed and discarded; its db
    file is replaced with a legacy-shape DB built by ``_build_legacy_db``.
    A new (uninitialized) SqliteGraphStore handle is constructed against the
    legacy file and bound to a new MaintenanceService. The registry entry
    is mutated to point at the legacy graph_store + the new
    MaintenanceService so the reload path inside ``migrate_vault`` closes
    the right handle.
    """
    db_path = Path(services.config.vault.brain_root) / "graph.db"
    await services.graph_store.close()
    db_path.unlink()
    _build_legacy_db(db_path)

    legacy_gs = SqliteGraphStore(db_path)
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=db_path,
        graph_store=legacy_gs,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
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


async def test_migrate_vault_applies_pending_alters_on_legacy_db(post_migration_vault):
    """A legacy-shape DB gets every MIGRATION_PLAN column added, and the
    report enumerates the entries that were applied."""
    registry, services, registry_service = post_migration_vault
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


async def test_migrate_vault_is_idempotent_on_current_schema(post_migration_vault):
    """A re-call against an already-migrated vault returns an empty
    report and does not touch the registry."""
    registry, services, registry_service = post_migration_vault
    # Build a maintenance service bound to the live (post-migration)
    # graph_store, mirroring what initialize_services already produced.
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=services.graph_store,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
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


async def test_migrate_vault_reloads_registry_in_place(post_migration_vault):
    """After migration, registry[vault_id] is a *new* SAGEServices
    instance whose graph_store serves reads against the now-migrated
    schema."""
    registry, services, registry_service = post_migration_vault
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


async def test_migrate_vault_reports_backfills(post_migration_vault):
    """Pending BACKFILL_PLAN entries appear in
    MigrationReport.backfills_applied alongside the column migrations."""
    registry, services, registry_service = post_migration_vault
    db_path, maintenance = await _swap_in_legacy_db(registry, services, registry_service)
    _seed_backfill_triggers(db_path)

    report = await maintenance.migrate_vault()

    # Both BACKFILL_PLAN entries activate on a legacy DB:
    # - document_tags: derived join table needs populating
    # - rationale_kind: re-classifies seeded edges with known prefixes
    assert "document_tags" in report.backfills_applied
    assert "rationale_kind" in report.backfills_applied


async def test_no_resource_warning_on_post_migration_teardown(tmp_path, monkeypatch):
    """regression: the post-migration teardown closes the registry's
    *current* graph_store, not the pre-migration ``services`` reference.

    Without the fix, ``maintenance.migrate_vault()`` swaps registry[vault_id]
    for a freshly-initialized SAGEServices and the test's local ``services``
    binding still points at the (already-closed) predecessor. Closing
    ``services.graph_store`` is a no-op; the new graph_store leaks and
    surfaces a ResourceWarning at GC time.

    We exercise the full cycle in an inner function so locals release on
    return, then force ``gc.collect()`` inside ``catch_warnings`` to make
    any deferred ``__del__`` finalizer surface as an error.
    """

    async def _exercise() -> None:
        async with _bootstrap_post_migration_vault(tmp_path, monkeypatch) as (
            registry,
            services,
            registry_service,
        ):
            _db_path, maintenance = await _swap_in_legacy_db(registry, services, registry_service)
            await maintenance.migrate_vault()
            # The exact teardown step under test: read the registry at
            # teardown time and close the current graph_store.
            await _close_registry_vault(registry, services.config.vault.id)

    await _exercise()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gc.collect()
        gc.collect()

    # Inspect the recorded warnings rather than filtering as error: pytest's
    # unraisable hook re-wraps ResourceWarning as PytestUnraisableExceptionWarning,
    # so a naive ``simplefilter("error", ResourceWarning)`` would let the
    # wrapped form through. Scan the formatted messages for the leak signature.
    leak_signature = "unclosed database"
    leaked = [str(w.message) for w in caught if leak_signature in str(w.message)]
    assert not leaked, f"unclosed sqlite3 connections surfaced after teardown: {leaked}"


# ---------------------------------------------------------------------------
# Outer-sequence atomicity: migrate_vault keeps self._graph_store live on
# migration / reload failure. Extends the inner-reload build-new-first
# guarantee outward over the close-migrate-reload sequence.
# ---------------------------------------------------------------------------


async def test_migrate_vault_keeps_graph_store_open_when_migration_fails(
    post_migration_vault, monkeypatch
):
    """B1: when the fresh-handle migration raises, the original
    ``self._graph_store`` stays initialized and the registry slot is
    unchanged. The exception propagates with no partial state.

    Trap (anti-coincidental): a close-then-migrate sequence runs
    ``await self._graph_store.close()`` BEFORE constructing the fresh
    handle, so on migration failure ``self._graph_store._executor is None``.
    A build-new-first sequence defers the close into the reload success
    path; the live graph_store remains initialized when migration fails.
    The ``_executor is not None`` assertion is the structural trap.
    """
    from sage.api.errors import SAGEError
    from sage.storage.graph_store import SqliteGraphStore as _RealGraphStore
    from sage.storage.migrations import Migration

    registry, services, registry_service = post_migration_vault

    # Establish baseline: the live graph_store is initialized.
    assert services.graph_store._executor is not None
    assert services.graph_store._all_connections

    # Force _detect_pending_work to report fake pending work so
    # migrate_vault enters the migration branch even though the live db is
    # already fully migrated.
    fake_pending = [
        Migration(
            table="documents",
            column="synthetic_pending_column_b1",
            ddl="ALTER TABLE documents ADD COLUMN synthetic_pending_column_b1 TEXT",
        )
    ]
    monkeypatch.setattr(
        "sage.services.maintenance.pending_migrations",
        lambda conn, plan=None: fake_pending,
    )

    # The fresh-handle's ``initialize(migrate=True)`` call goes through the
    # ``SqliteGraphStore`` binding in ``sage.services.maintenance``'s module
    # namespace. Replace just that binding with a subclass that raises on
    # migrate=True. The original ``services.graph_store`` was constructed
    # earlier through a different module path, so it is unaffected by the
    # monkeypatch.
    class FailingFreshGraphStore(_RealGraphStore):
        async def initialize(self, migrate: bool = False) -> None:
            if migrate:
                raise SAGEError(
                    code="schema_migration_required",
                    message="simulated migration failure for outer-sequence atomicity test",
                    status_code=409,
                )
            await super().initialize(migrate=migrate)

    monkeypatch.setattr("sage.services.maintenance.SqliteGraphStore", FailingFreshGraphStore)

    # Build a maintenance service against the live graph_store.
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=services.graph_store,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )

    pre_call_services = registry[services.config.vault.id]

    with pytest.raises(SAGEError, match="simulated migration failure for outer-sequence"):
        await maintenance.migrate_vault()

    # (a) The original graph_store stays live. A close-then-migrate
    # sequence would have run ``close()`` before the failure point,
    # leaving ``_executor=None`` and ``_all_connections=[]``. Build-new-
    # first leaves the live store untouched on the migration-failure path.
    assert services.graph_store._executor is not None, (
        "self._graph_store was closed before the migration failure; "
        "build-new-first ordering not enforced"
    )
    assert services.graph_store._all_connections, (
        "self._graph_store has no live connections; close() was called"
    )
    # Behavioural co-assertion per TEST-SAGE-BH-137: the CAS-ADR-036 dispatch
    # barrier would raise if close() had run. A successful dispatch confirms
    # the live store still serves operations, not just that its bookkeeping
    # fields look healthy.
    live_docs = await services.graph_store.list_all_documents()
    assert isinstance(live_docs, list)

    # (b) Registry slot identity unchanged.
    assert registry[services.config.vault.id] is pre_call_services


async def test_migrate_vault_keeps_graph_store_open_when_post_migration_reload_fails(
    post_migration_vault, monkeypatch
):
    """B2: after migration succeeds on disk, if the subsequent registry
    reload fails, ``self._graph_store`` stays live and the registry slot is
    unchanged. The inner-reload build-new-first guarantee leaves the old
    services installed on reload failure, and the outer-sequence wrap
    ensures that old graph_store has not been pre-emptively closed.

    Trap (anti-coincidental): a close-then-migrate-then-reload sequence
    closes ``self._graph_store`` BEFORE invoking reload, so a reload
    failure leaves the registry slot pointing at services with a closed
    graph_store. Deferring the close into reload's success path leaves
    the live store open on the reload-failure path.
    """
    from sage.api.errors import SAGEError
    from sage.storage.graph_store import SqliteGraphStore as _RealGraphStore
    from sage.storage.migrations import Migration

    registry, services, registry_service = post_migration_vault

    assert services.graph_store._executor is not None

    # Force fake pending work so migrate_vault enters the migration branch.
    fake_pending = [
        Migration(
            table="documents",
            column="synthetic_pending_column_b2",
            ddl="ALTER TABLE documents ADD COLUMN synthetic_pending_column_b2 TEXT",
        )
    ]
    monkeypatch.setattr(
        "sage.services.maintenance.pending_migrations",
        lambda conn, plan=None: fake_pending,
    )

    # Migration step succeeds via a no-op fresh-handle subclass: skip the
    # DDL but pretend to initialize. We want the failure to come from the
    # POST-migration reload step, not the migration itself.
    class NoOpFreshGraphStore(_RealGraphStore):
        async def initialize(self, migrate: bool = False) -> None:
            # Synthetic-pending entries above don't reflect real schema
            # work; skip applying them.
            return None

    monkeypatch.setattr("sage.services.maintenance.SqliteGraphStore", NoOpFreshGraphStore)

    # Inject the reload failure via the standard idiom: monkeypatch
    # ``initialize_services`` at both possible call sites. ``reload_vault_in_registry``
    # imports it from ``sage.mcp_init``, so that is the primary site.
    import sage.mcp_init as _mcp_init

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated post-migration reload failure for outer-sequence atomicity",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)

    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=services.graph_store,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )

    pre_call_services = registry[services.config.vault.id]

    with pytest.raises(SAGEError, match="simulated post-migration reload failure"):
        await maintenance.migrate_vault()

    # (a) The original graph_store stays live across the reload-failure path.
    # A close-then-migrate sequence would have run ``self._graph_store.close()``
    # before fresh.initialize, so by the time reload raised, _executor was None.
    assert services.graph_store._executor is not None, (
        "self._graph_store was closed before the post-migration reload; "
        "the close must defer to reload's success path"
    )
    assert services.graph_store._all_connections, (
        "self._graph_store has no live connections after reload failure"
    )
    # Behavioural co-assertion per TEST-SAGE-BH-137: the CAS-ADR-036 dispatch
    # barrier would raise if close() had run. A successful dispatch confirms
    # the live store survived the outer-sequence reload failure.
    live_docs = await services.graph_store.list_all_documents()
    assert isinstance(live_docs, list)

    # (b) Registry slot identity unchanged: the inner-reload build-new-first
    # guarantee preserves the old SAGEServices reference when
    # initialize_services raises.
    assert registry[services.config.vault.id] is pre_call_services


# ---------------------------------------------------------------------------
# MaintenanceService.detect_drift
# ---------------------------------------------------------------------------


def _drift_doc(doc_id: str, content_hash: str, version_label: str | None = None) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Drift test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"drift/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash=content_hash,
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        version_label=version_label,
    )


def _drift_edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    *,
    edge_type: EdgeType = EdgeType.DERIVED_FROM,
    synced_from_version: str | None = None,
    synced_from_content_hash: str | None = None,
) -> Edge:
    return Edge(
        id=edge_id,
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_type,
        source_valid_from_version=source_id if edge_type == EdgeType.DERIVED_FROM else None,
        created_at=datetime.now(timezone.utc),
        synced_from_version=synced_from_version,
        synced_from_content_hash=synced_from_content_hash,
    )


def _hash(suffix: str) -> str:
    """Canonical sha256-shaped hash from a short test suffix.

    Caller passes a single hex char; we repeat to make a 64-char digest.
    Non-hex inputs fall back to a deterministic hex digest derived from
    the suffix so test signatures stay readable but the value still
    validates against `^sha256:[0-9a-f]{64}$`.
    """
    import hashlib

    if len(suffix) == 1 and suffix in "0123456789abcdef":
        return "sha256:" + suffix * 64
    return "sha256:" + hashlib.sha256(f"t0111-test:{suffix}".encode()).hexdigest()


async def test_t0111_detect_drift_multi_basket(post_migration_vault):
    """T-DD-multi: one fixture, four edges, three expected baskets.

    - A: hash matches current head → absent from report.
    - B: hash differs from head → content_drift.
    - C: hash matches head but synced_from_version != head_id →
      chain_advanced_no_content_change.
    - D: both fields NULL → recorded_null.
    """
    registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=gs,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )

    # Chain T1 (tail) → T2 (head, supersedes T1).
    t1_hash = _hash("1")
    t2_hash = _hash("2")
    wrong_hash = _hash("f")
    await gs.insert_document(_drift_doc("deadbeef_t1", t1_hash, "v1"))
    await gs.insert_document(_drift_doc("cafebabe_t2", t2_hash, "v2"))
    # T2 supersedes T1 (source=T2 is newer).
    await gs.insert_edge(
        _drift_edge(
            "11111111-1111-4111-8111-111111111111",
            source_id="cafebabe_t2",
            target_id="deadbeef_t1",
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    # Four source docs to dodge the (source_id, target_id, edge_type) unique constraint.
    for sid in ("aaaaaaaa_a", "bbbbbbbb_b", "cccccccc_c", "dddddddd_d"):
        await gs.insert_document(_drift_doc(sid, _hash(sid[0])))

    # A: current — recorded matches head exactly.
    await gs.insert_edge(
        _drift_edge(
            "22222222-2222-4222-8222-222222222222",
            source_id="aaaaaaaa_a",
            target_id="cafebabe_t2",
            synced_from_version="cafebabe_t2",
            synced_from_content_hash=t2_hash,
        )
    )
    # B: content_drift — hash diverged from head.
    await gs.insert_edge(
        _drift_edge(
            "33333333-3333-4333-8333-333333333333",
            source_id="bbbbbbbb_b",
            target_id="cafebabe_t2",
            synced_from_version="cafebabe_t2",
            synced_from_content_hash=wrong_hash,
        )
    )
    # C: chain_advanced_no_content_change — recorded version != head, hash matches.
    await gs.insert_edge(
        _drift_edge(
            "44444444-4444-4444-8444-444444444444",
            source_id="cccccccc_c",
            target_id="cafebabe_t2",
            synced_from_version="deadbeef_t1",
            synced_from_content_hash=t2_hash,
        )
    )
    # D: recorded_null — neither field set.
    await gs.insert_edge(
        _drift_edge(
            "55555555-5555-4555-8555-555555555555",
            source_id="dddddddd_d",
            target_id="cafebabe_t2",
        )
    )

    report = await maintenance.detect_drift()

    assert isinstance(report, DriftReport)
    assert report.vault_id == services.config.vault.id
    # A is absent → 3 entries (one each B, C, D).
    assert len(report.entries) == 3
    bases = {e.edge_id: e.staleness_basis for e in report.entries}
    assert bases["33333333-3333-4333-8333-333333333333"] == StalenessBasis.CONTENT_DRIFT
    assert (
        bases["44444444-4444-4444-8444-444444444444"]
        == StalenessBasis.CHAIN_ADVANCED_NO_CONTENT_CHANGE
    )
    assert bases["55555555-5555-4555-8555-555555555555"] == StalenessBasis.RECORDED_NULL
    # A's id should NOT be in the report.
    assert "22222222-2222-4222-8222-222222222222" not in bases
    # Summary counts the basis values directly.
    assert report.summary["content_drift"] == 1
    assert report.summary["chain_advanced_no_content_change"] == 1
    assert report.summary["recorded_null"] == 1
    assert report.summary["chain_nonlinear"] == 0


async def test_t0111_detect_drift_nonlinear_chain(post_migration_vault):
    """T-DD-nonlinear: target with a forked chain (two heads) is reported
    with staleness_basis=chain_nonlinear; head fields are null and
    competing_head_count carries the fork width."""
    registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=gs,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )

    # Fork: T1 has TWO superseding successors → two heads.
    await gs.insert_document(_drift_doc("deadbeef_t1", _hash("1"), "v1"))
    await gs.insert_document(_drift_doc("cafebabe_2a", _hash("a"), "v2a"))
    await gs.insert_document(_drift_doc("cafef00d_2b", _hash("b"), "v2b"))
    await gs.insert_edge(
        _drift_edge(
            "11111111-1111-4111-8111-aaaaaaaaaaaa",
            source_id="cafebabe_2a",
            target_id="deadbeef_t1",
            edge_type=EdgeType.SUPERSEDES,
        )
    )
    await gs.insert_edge(
        _drift_edge(
            "11111111-1111-4111-8111-bbbbbbbbbbbb",
            source_id="cafef00d_2b",
            target_id="deadbeef_t1",
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    # Consumer edge targeting any chain member.
    await gs.insert_document(_drift_doc("aaaaaaaa_s", _hash("s")))
    await gs.insert_edge(
        _drift_edge(
            "99999999-9999-4999-8999-999999999999",
            source_id="aaaaaaaa_s",
            target_id="deadbeef_t1",  # target = tail; chain has 2 heads
            synced_from_version="cafebabe_2a",
            synced_from_content_hash=_hash("a"),
        )
    )

    report = await maintenance.detect_drift()
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.staleness_basis == StalenessBasis.CHAIN_NONLINEAR
    assert entry.current_head_id is None
    assert entry.competing_head_count == 2
    assert report.summary["chain_nonlinear"] == 1


async def test_t0111_detect_drift_version_only(post_migration_vault):
    """T-DD-version-only: edges with synced_from_version set but
    synced_from_content_hash NULL. Three sub-cases — current (recorded
    == head), chain_advanced (recorded != head, recorded.hash == head.hash),
    content_drift (recorded != head, recorded.hash != head.hash)."""
    registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maintenance = MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=gs,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )

    # Chain: T1 (oldest, hash_old) → T2 (middle, SAME hash as head) → T3 (head, hash_head).
    # T2 and T3 share a hash to make chain_advanced_no_content_change observable.
    same_hash = _hash("c")
    t1_hash = _hash("1")
    await gs.insert_document(_drift_doc("deadbeef_t1", t1_hash, "v1"))
    await gs.insert_document(_drift_doc("cafebabe_t2", same_hash, "v2"))
    await gs.insert_document(_drift_doc("cafef00d_t3", same_hash, "v3"))
    # T2 supersedes T1, T3 supersedes T2.
    await gs.insert_edge(
        _drift_edge(
            "11111111-1111-4111-8111-111111111111",
            source_id="cafebabe_t2",
            target_id="deadbeef_t1",
            edge_type=EdgeType.SUPERSEDES,
        )
    )
    await gs.insert_edge(
        _drift_edge(
            "11111111-1111-4111-8111-222222222222",
            source_id="cafef00d_t3",
            target_id="cafebabe_t2",
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    # Three consumer source docs.
    for sid in ("aaaaaaaa_a", "bbbbbbbb_b", "cccccccc_c"):
        await gs.insert_document(_drift_doc(sid, _hash(sid[0])))

    # Sub 1: recorded == head_id, hash NULL → current (absent).
    await gs.insert_edge(
        _drift_edge(
            "22222222-2222-4222-8222-222222222222",
            source_id="aaaaaaaa_a",
            target_id="cafef00d_t3",
            synced_from_version="cafef00d_t3",
        )
    )
    # Sub 2: recorded != head, recorded.hash == head.hash → chain_advanced.
    await gs.insert_edge(
        _drift_edge(
            "33333333-3333-4333-8333-333333333333",
            source_id="bbbbbbbb_b",
            target_id="cafef00d_t3",
            synced_from_version="cafebabe_t2",  # T2 shares hash with head T3
        )
    )
    # Sub 3: recorded != head, recorded.hash != head.hash → content_drift.
    await gs.insert_edge(
        _drift_edge(
            "44444444-4444-4444-8444-444444444444",
            source_id="cccccccc_c",
            target_id="cafef00d_t3",
            synced_from_version="deadbeef_t1",  # T1 has DIFFERENT hash
        )
    )

    report = await maintenance.detect_drift()
    assert len(report.entries) == 2  # sub-2 + sub-3; sub-1 is current
    bases = {e.edge_id: e.staleness_basis for e in report.entries}
    assert (
        bases["33333333-3333-4333-8333-333333333333"]
        == StalenessBasis.CHAIN_ADVANCED_NO_CONTENT_CHANGE
    )
    assert bases["44444444-4444-4444-8444-444444444444"] == StalenessBasis.CONTENT_DRIFT
    assert "22222222-2222-4222-8222-222222222222" not in bases


# ---------------------------------------------------------------------------
# MaintenanceService.verify_vault_source_files
# ---------------------------------------------------------------------------


def _src_doc(
    doc_id: str,
    content_hash: str,
    *,
    source_path: str,
    lifecycle_status: str = "active",
    version_label: str | None = None,
) -> Document:
    """A Document for source-file-audit tests, parameterized on the fields
    the audit reads: source_path, lifecycle_status, source_content_hash."""
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Source test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=source_path,
        lifecycle_status=lifecycle_status,
        source_content_hash=content_hash,
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        version_label=version_label,
    )


def _sha256_of(content: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


def _maintenance_for(services, registry_service) -> MaintenanceService:
    return MaintenanceService(
        vault_id=services.config.vault.id,
        db_path=Path(services.config.vault.brain_root) / "graph.db",
        graph_store=services.graph_store,
        config=services.config,
        registry_service=registry_service,
        content_store=services.content_store,
    )


def _write_source(services, source_path: str, content: bytes) -> Path:
    """Write a real file at storage_root/source_path (mkdir parents)."""
    p = Path(services.config.vault.storage_root) / source_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


async def test_verify_source_files_clean_vault_returns_empty(post_migration_vault):
    """Happy path: every document's source file present → empty report,
    all healthy."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    for i in range(3):
        sp = f"imports/deadbeef_d{i}.md"
        body = f"body {i}".encode()
        _write_source(services, sp, body)
        await gs.insert_document(_src_doc(f"deadbeef_d{i}", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.vault_id == services.config.vault.id
    assert report.total_documents_checked == 3
    assert report.check_hashes is False
    assert report.entries == []
    assert report.summary == {"healthy": 3, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_flags_missing_file(post_migration_vault):
    """The precipitating incident: one document's backing file is absent.
    It is the only entry, classified `missing`, and the present docs stay
    healthy."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    for did, present in (("aaaaaaaa_a", True), ("bbbbbbbb_b", False), ("cccccccc_c", True)):
        sp = f"imports/{did}.md"
        body = f"{did} body".encode()
        if present:
            _write_source(services, sp, body)
        await gs.insert_document(_src_doc(did, _sha256_of(body), source_path=sp))

    # Positive control: the present files exist; the flagged one does not.
    root = Path(services.config.vault.storage_root)
    assert (root / "imports/aaaaaaaa_a.md").exists()
    assert (root / "imports/cccccccc_c.md").exists()
    assert not (root / "imports/bbbbbbbb_b.md").exists()

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.total_documents_checked == 3
    assert report.summary == {"healthy": 2, "missing": 1, "hash_mismatch": 0}
    assert [e.document_id for e in report.entries] == ["bbbbbbbb_b"]
    entry = report.entries[0]
    assert entry.integrity_status == "missing"
    assert entry.source_path == "imports/bbbbbbbb_b.md"
    assert entry.lifecycle_status == "active"
    assert entry.observed_content_hash is None


async def test_verify_source_files_surfaces_archived_missing(post_migration_vault):
    """Scope is all lifecycle states: an archived record with a missing
    file is surfaced (the incident's residual was an archived version)."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    await gs.insert_document(
        _src_doc(
            "aaaaaaaa_old",
            _sha256_of(b"x"),
            source_path="imports/archived.md",
            lifecycle_status="archived",
            version_label="v1",
        )
    )

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.total_documents_checked == 1
    assert report.summary["missing"] == 1
    entry = report.entries[0]
    assert entry.document_id == "aaaaaaaa_old"
    assert entry.lifecycle_status == "archived"
    assert entry.version_label == "v1"


async def test_verify_source_files_existence_mode_ignores_hash_drift(post_migration_vault):
    """check_hashes=False never reads content: a present file whose bytes
    do not match the recorded hash is NOT flagged."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    sp = "imports/deadbeef_h.md"
    _write_source(services, sp, b"actual content")
    await gs.insert_document(
        _src_doc("deadbeef_h", _sha256_of(b"different content"), source_path=sp)
    )

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.entries == []
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_hash_mode_flags_mismatch(post_migration_vault):
    """check_hashes=True: a present file whose bytes diverge from the
    recorded hash surfaces as `hash_mismatch` with the observed hash."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    sp = "imports/deadbeef_h.md"
    _write_source(services, sp, b"actual content")
    expected = _sha256_of(b"different content")
    await gs.insert_document(_src_doc("deadbeef_h", expected, source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.check_hashes is True
    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 1}
    entry = report.entries[0]
    assert entry.integrity_status == "hash_mismatch"
    assert entry.expected_content_hash == expected
    assert entry.observed_content_hash == _sha256_of(b"actual content")
    assert entry.observed_content_hash != entry.expected_content_hash


async def test_verify_source_files_hash_mode_clean_when_matching(post_migration_vault):
    """check_hashes=True with a matching on-disk file → no entry. Guards
    against the mismatch path firing on every file."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    sp = "imports/deadbeef_h.md"
    body = b"matching content"
    _write_source(services, sp, body)
    await gs.insert_document(_src_doc("deadbeef_h", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.entries == []
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_hash_mode_missing_stays_missing(post_migration_vault):
    """A missing file under check_hashes=True classifies as `missing` (not
    a hash error) with a null observed hash."""
    _registry, services, registry_service = post_migration_vault
    gs = services.graph_store
    maint = _maintenance_for(services, registry_service)

    # No file written for this doc.
    await gs.insert_document(
        _src_doc("deadbeef_g", _sha256_of(b"x"), source_path="imports/gone.md")
    )

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 0, "missing": 1, "hash_mismatch": 0}
    entry = report.entries[0]
    assert entry.integrity_status == "missing"
    assert entry.observed_content_hash is None


# ============================================================================
# optimize_vault_content_store tests
# ============================================================================


@contextlib.asynccontextmanager
async def _bootstrap_lancedb_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Async context manager: ephemeral vault wired to a real LanceDB store.

    Yields ``(registry, services, vault_dir)``. Distinct from
    ``_bootstrap_post_migration_vault`` (which substitutes
    StubContentStore for ~700 unrelated test paths): the optimize
    operation needs the LanceDB substrate to observe real on-disk
    reclamation. The vault's MaintenanceService is rewired so its
    audit log lands under ``tmp_path`` rather than the canonical
    ``~/sage_vaults/<vault_id>/`` location (which doesn't exist in
    test environments).
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = _minimal_config(tmp_path)
    # get_stats reads the maintenance log from config_path_for_vault(...).parent;
    # redirect it to tmp_path so the reader agrees with the writer's
    # _vault_dir override below (the canonical ~/sage_vaults/<id>/ path does
    # not exist in test environments).
    monkeypatch.setattr(
        "sage.services.vault_config.config_path_for_vault",
        lambda vault_id: tmp_path / "vault_config.yaml",
    )
    registry: dict[str, SAGEServices] = {}
    registry_service = VaultRegistryService(registry, initialize_services)
    async with initialize_services_for_test(
        config,
        migrate=True,
        registry_service=registry_service,
        content_store_factory=lambda brain: LanceDBContentStore(brain),
    ) as services:
        registry[config.vault.id] = services
        services.maintenance_service._vault_dir = tmp_path
        yield registry, services, tmp_path


@pytest.fixture
async def lancedb_vault(tmp_path, monkeypatch):
    """Pytest-fixture form of ``_bootstrap_lancedb_vault``.

    Yields ``(registry, services, vault_dir)``. Mirrors the teardown
    contract of ``post_migration_vault``: closes whichever graph_store
    currently occupies ``registry[vault_id]`` so test-induced reloads
    don't leak handles.
    """
    async with _bootstrap_lancedb_vault(tmp_path, monkeypatch) as (
        registry,
        services,
        vault_dir,
    ):
        try:
            yield registry, services, vault_dir
        finally:
            await _close_registry_vault(registry, services.config.vault.id)


def _make_chunk(document_id: str, revision: int) -> Chunk:
    """Build a synthetic chunk with deterministic content per (doc, rev)."""
    return Chunk(
        document_id=document_id,
        heading_path="root",
        content=f"{document_id} content revision {revision}",
        chunk_index=0,
    )


async def _churn_chunks(content_store: LanceDBContentStore, cycles: int) -> None:
    """Drive ``cycles`` mutations against the chunks table.

    Each cycle replaces a document's chunks (an internal delete + add
    inside ``index_chunks``), so a vault that began empty accumulates
    roughly ``cycles`` retained LanceDB versions plus header / FTS
    rebuilds. Spread across three doc_ids so the fragment count grows
    in addition to the version count.
    """
    doc_ids = ("doc_a", "doc_b", "doc_c")
    for i in range(cycles):
        doc = doc_ids[i % len(doc_ids)]
        await content_store.index_chunks(doc, [_make_chunk(doc, i)])
    # One outright deletion so a deletion-shaped manifest exists.
    await content_store.remove_document("doc_c")


async def test_optimize_content_store_reclaims_disk_after_churn(lancedb_vault):
    """Churn → optimize → bytes/versions/fragments collapse;
    audit log records the call.
    """
    _registry, services, vault_dir = lancedb_vault
    content_store = services.content_store
    maintenance = services.maintenance_service

    await _churn_chunks(content_store, cycles=30)

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    assert isinstance(report, OptimizeContentStoreReport)
    assert report.vault_id == services.config.vault.id
    # Reclamation evidence: at least one of the three signals must
    # show measurable shrinkage. Bytes is the headline metric; versions
    # are the strongest signal because cleanup_older_than_days=0
    # prunes everything but the latest.
    assert report.pre_versions > report.post_versions, (
        f"expected version-history shrinkage, got pre={report.pre_versions} "
        f"post={report.post_versions}"
    )
    assert report.post_versions >= 1, "the latest version is never removed"
    assert report.pre_bytes > report.post_bytes, (
        f"expected on-disk byte shrinkage, got pre={report.pre_bytes} post={report.post_bytes}"
    )
    assert report.bytes_reclaimed == report.pre_bytes - report.post_bytes
    assert report.cleanup_older_than_days == 0
    assert report.finished_at >= report.started_at

    # Table remains queryable after optimize.
    assert await content_store.count_chunks() > 0

    # Audit log: one JSONL line that round-trips and matches the report.
    audit_path = vault_dir / ".maintenance_log.jsonl"
    assert audit_path.exists(), "audit log should be written"
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got {len(lines)}"
    entry = json.loads(lines[0])
    assert entry["operation"] == "optimize_vault_content_store"
    assert entry["vault_id"] == report.vault_id
    assert entry["cleanup_older_than_days"] == 0
    assert entry["pre_bytes"] == report.pre_bytes
    assert entry["post_bytes"] == report.post_bytes
    assert entry["pre_versions"] == report.pre_versions
    assert entry["post_versions"] == report.post_versions


async def test_count_retained_versions_rises_with_churn(lancedb_vault):
    """Retained-version count rises with un-optimized write churn.

    This is the read-only signal behind the dashboard bloat indicator:
    each write mints a LanceDB dataset version (header / FTS rebuild
    included), so an un-optimized vault accumulates versions monotonically.
    A fresh churned vault must report more than the single baseline version.
    """
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store

    await _churn_chunks(content_store, cycles=15)

    assert await content_store.count_retained_versions() > 1


async def test_get_stats_surfaces_retained_version_count(lancedb_vault):
    """get_stats exposes the retained-version count to the dashboard.

    Regression guard: the value the HTTP stats endpoint returns must equal
    the content store's own read-only measure, not a constant.
    """
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store

    await _churn_chunks(content_store, cycles=15)

    stats = await services.vault_config_service.get_stats()
    assert stats.content_store_version_count == await content_store.count_retained_versions()
    assert stats.content_store_version_count > 1


async def test_count_small_fragments_rises_with_churn(lancedb_vault):
    """Small-fragment count rises with un-optimized write churn.

    Companion signal to the retained-version count: un-compacted small
    fragments accumulate as small writes land without an optimize. Unlike a
    total fragment count, this measure stays near zero on a healthy store
    regardless of corpus size, so it is self-calibrating. A churned vault
    must report at least one small fragment.
    """
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store

    await _churn_chunks(content_store, cycles=15)

    assert await content_store.count_small_fragments() > 0


async def test_get_stats_surfaces_small_fragment_count(lancedb_vault):
    """get_stats exposes the small-fragment count to the dashboard.

    Regression guard: the value the HTTP stats endpoint returns must equal
    the content store's own read-only measure, not a constant.
    """
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store

    await _churn_chunks(content_store, cycles=15)

    stats = await services.vault_config_service.get_stats()
    assert stats.content_store_small_fragment_count == await content_store.count_small_fragments()
    assert stats.content_store_small_fragment_count > 0


async def test_read_last_optimize_summary_none_when_no_log(tmp_path):
    """No maintenance log on disk → reader returns None (never-optimized vault)."""
    from sage.services.maintenance_log import read_last_optimize_summary

    assert read_last_optimize_summary(tmp_path) is None


async def test_read_last_optimize_summary_after_optimize(lancedb_vault):
    """Round-trip: a real optimize writes the audit record and the reader
    returns a summary whose fields match that record.

    Guards writer/reader key agreement -- renaming a key in either half
    breaks this test.
    """
    from sage.services.maintenance_log import read_last_optimize_summary

    _registry, services, vault_dir = lancedb_vault
    content_store = services.content_store
    maintenance = services.maintenance_service

    await _churn_chunks(content_store, cycles=30)
    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    summary = read_last_optimize_summary(vault_dir)
    assert summary is not None
    assert summary.bytes_reclaimed == report.bytes_reclaimed
    assert summary.versions_cleaned == report.pre_versions - report.post_versions
    assert summary.fragments_merged == report.pre_fragments - report.post_fragments
    # `at` parses the record timestamp; it must fall within the optimize window.
    assert summary.at >= report.started_at


async def test_read_last_optimize_summary_picks_most_recent_and_filters(tmp_path):
    """Reader returns the most-recent optimize record (append-order) and
    ignores non-optimize lines.
    """
    from sage.services.maintenance_log import (
        MAINTENANCE_LOG_FILENAME,
        read_last_optimize_summary,
    )

    older = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "operation": "optimize_vault_content_store",
        "bytes_reclaimed": 100,
        "pre_versions": 9,
        "post_versions": 4,
        "pre_fragments": 8,
        "post_fragments": 3,
    }
    other = {"timestamp": "2026-01-02T00:00:00+00:00", "operation": "purge_document"}
    newer = {
        "timestamp": "2026-02-02T00:00:00+00:00",
        "operation": "optimize_vault_content_store",
        "bytes_reclaimed": 999,
        "pre_versions": 20,
        "post_versions": 2,
        "pre_fragments": 30,
        "post_fragments": 5,
    }
    log = tmp_path / MAINTENANCE_LOG_FILENAME
    log.write_text("\n".join(json.dumps(r) for r in (older, other, newer)) + "\n")

    summary = read_last_optimize_summary(tmp_path)
    assert summary is not None
    assert summary.bytes_reclaimed == 999
    assert summary.versions_cleaned == 18
    assert summary.fragments_merged == 25


async def test_get_stats_surfaces_last_optimize(lancedb_vault):
    """get_stats surfaces the last-optimize summary; None before any optimize."""
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store
    maintenance = services.maintenance_service

    await _churn_chunks(content_store, cycles=30)

    stats_before = await services.vault_config_service.get_stats()
    assert stats_before.last_optimize is None

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    stats_after = await services.vault_config_service.get_stats()
    assert stats_after.last_optimize is not None
    assert stats_after.last_optimize.bytes_reclaimed == report.bytes_reclaimed


async def test_optimize_content_store_is_noop_on_clean_table(lancedb_vault):
    """Optimize converges to a fixpoint; at the fixpoint a further call
    reports no further reclamation and the table remains queryable.

    Incremental FTS maintenance leaves newly-written rows in an unindexed
    delta that the first optimize folds into the index; that fold can mint
    a dataset version a subsequent optimize then prunes, so convergence may
    take more than one pass. The invariant under test is that optimize
    *does* converge and that at the fixpoint it is a true no-op.
    """
    _registry, services, _vault_dir = lancedb_vault
    content_store = services.content_store
    maintenance = services.maintenance_service

    await _churn_chunks(content_store, cycles=30)

    # Optimize until it reaches a fixpoint (no version or fragment change).
    report = None
    for _ in range(6):
        report = await maintenance.optimize_content_store(cleanup_older_than_days=0)
        if (
            report.pre_versions == report.post_versions
            and report.pre_fragments == report.post_fragments
        ):
            break
    else:
        raise AssertionError(
            f"optimize did not reach a fixpoint within 6 passes; last "
            f"versions={report.pre_versions}->{report.post_versions} "
            f"fragments={report.pre_fragments}->{report.post_fragments}"
        )

    # At the fixpoint, a further optimize reclaims nothing.
    assert report.pre_versions == report.post_versions
    # Fragments may already be merged; if they were, count stays put.
    assert report.pre_fragments == report.post_fragments
    # bytes_reclaimed is bounded at zero so a tiny rewrite can't go negative.
    assert report.bytes_reclaimed >= 0
    # Table remains queryable.
    assert await content_store.count_chunks() > 0


@pytest.mark.parametrize(
    "cleanup_days,expect_pruning",
    [(0, True), (999999, False)],
)
async def test_optimize_content_store_threshold_controls_pruning(
    tmp_path, monkeypatch, cleanup_days: int, expect_pruning: bool
):
    """cleanup_older_than_days=0 prunes everything-but-latest;
    cleanup_older_than_days=999999 prunes nothing (recent test churn
    is well within any sane retention window).

    Anti-coincidental-pass note: a code path that ignores
    cleanup_older_than_days and passes LanceDB's default (7 days)
    cannot be discriminated by observed pruning alone in a
    synchronous test (all version timestamps are <1s old, so the 7-day
    default prunes nothing either). The honoring of the parameter is
    additionally asserted through the report and audit-log fields,
    and via the focused observable here that days=0 prunes a churned
    vault aggressively while days=999999 prunes no historic version.
    """
    async with _bootstrap_lancedb_vault(tmp_path, monkeypatch) as (
        _registry,
        services,
        _vault_dir,
    ):
        content_store = services.content_store
        maintenance = services.maintenance_service

        try:
            await _churn_chunks(content_store, cycles=10)

            report = await maintenance.optimize_content_store(cleanup_older_than_days=cleanup_days)
            assert report.cleanup_older_than_days == cleanup_days

            if expect_pruning:
                assert report.pre_versions > report.post_versions, (
                    f"days=0 should prune historic versions; got "
                    f"pre={report.pre_versions} post={report.post_versions}"
                )
                assert report.post_versions >= 1
            else:
                # Recent churn cannot be older than 999999 days;
                # therefore no historic version may be pruned. Optimize
                # may add new versions for compaction work, so
                # post_versions >= pre_versions is the right bound (it
                # is never less when no pruning occurred).
                assert report.post_versions >= report.pre_versions, (
                    f"days={cleanup_days} must not prune any historic version; "
                    f"got pre={report.pre_versions} post={report.post_versions}"
                )
        finally:
            await _close_registry_vault(_registry, services.config.vault.id)


async def test_optimize_content_store_rejects_negative_days(lancedb_vault):
    """Negative cleanup_older_than_days is a service-level
    precondition violation -- the validation must live on the service
    method itself, not only on the Pydantic request model, because the
    MCP-tool surface calls the service directly.
    """
    _registry, services, vault_dir = lancedb_vault

    with pytest.raises(ValueError, match="cleanup_older_than_days must be >= 0"):
        await services.maintenance_service.optimize_content_store(cleanup_older_than_days=-1)

    # The audit log must NOT be appended on a rejected call.
    audit_path = vault_dir / ".maintenance_log.jsonl"
    assert not audit_path.exists(), "audit log should not be written on a rejected call"


# ---------------------------------------------------------------------------
# Backend-aware migrate_vault (CAS-ADR-042): the SQLite detect-then-apply path
# is meaningful only for the embedded backend. On a Postgres-backed vault the
# schema is provisioned externally, so migrate_vault must short-circuit that
# path entirely -- no local file, no raise, empty report -- while the
# backend-agnostic tier3 scan still runs. The existing embedded-path tests
# above (constructed without storage_backend, defaulting to "embedded") are the
# regression guard for the other direction.
# ---------------------------------------------------------------------------


async def test_migrate_vault_postgres_backend_is_noop_without_touching_disk(tmp_path, monkeypatch):
    """On a Postgres-backed vault, migrate_vault returns an empty
    MigrationReport, raises nothing, creates no local graph.db, and never
    constructs a SqliteGraphStore.

    The Postgres schema is provisioned externally and
    ``PostgresGraphStore.initialize(migrate=True)`` is a deliberate no-op, so
    there is nothing for the SQLite detect-then-apply path to do. Touching
    ``brain_root/graph.db`` at all would open (and create) a stray SQLite file
    that is not the real store.
    """
    config = _minimal_config(tmp_path)
    db_path = tmp_path / "graph.db"
    assert not db_path.exists()

    # Trip wire: constructing a SqliteGraphStore on the postgres branch is a
    # regression (it is the crash surface on a real Postgres vault). Patch the
    # name the service resolves so any construction fails loudly.
    class _NoSqliteGraphStore:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SqliteGraphStore must not be constructed on the postgres branch")

    monkeypatch.setattr("sage.services.maintenance.SqliteGraphStore", _NoSqliteGraphStore)

    maintenance = MaintenanceService(
        vault_id=config.vault.id,
        db_path=db_path,
        graph_store=StubGraphStore(),
        config=config,
        registry_service=None,
        content_store=StubContentStore(),
        storage_backend="postgres",
    )

    report = await maintenance.migrate_vault()

    assert report.vault_id == config.vault.id
    assert report.columns_added == []
    assert report.backfills_applied == []
    assert report.tier3_uniqueness_activations == []
    assert report.tier3_uniqueness_collisions == []
    # Acceptance criterion: no brain_root/graph.db is created as a side effect.
    # The detect path's first act (sqlite3.connect) would create this file, so
    # its absence proves the SQLite path was never entered.
    assert not db_path.exists()
