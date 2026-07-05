"""Unit tests for MaintenanceService (CAS-ADR-029).

Exercises the maintenance surface over the Postgres storage binding
(CAS-ADR-042):

1. ``migrate_vault`` is a schema no-op — the schema is provisioned
   externally, so the report carries no column or backfill work, no local
   file is touched, and the registry is never reloaded — while the
   backend-agnostic tier3-uniqueness scan (CAS-ADR-031) still runs on
   every call.
2. ``detect_drift`` classifies sync provenance against supersession-chain
   heads into the four StalenessBasis buckets.
3. ``verify_vault_source_files`` audits vault-local source files for
   presence and (optionally) hash agreement.
4. ``optimize_content_store`` delegates to the ContentStore port, shapes
   the report from the store's snapshot, and appends the JSONL audit
   record. Store-level reclamation is covered by the content-store test
   modules; the tests here pin the service-level contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sage.adapters.interfaces import ContentStoreOptimizeSnapshot
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType, StalenessBasis
from sage.models.schemas import (
    Document,
    DriftReport,
    Edge,
    MigrationReport,
    OptimizeContentStoreReport,
)
from sage.services.maintenance import MaintenanceService
from sage.storage.tier3_uniqueness import tier3_unique_index_name
from tests.sage.test_tier3_uniqueness import (
    _config_dict_with_unique_keys,
    _make_ticket_doc,
)


def _maintenance_for(
    graph_store,
    config: VaultConfig,
    *,
    content_store=None,
    registry_service=None,
    vault_dir: Path | None = None,
) -> MaintenanceService:
    """Build a MaintenanceService over the given store pair.

    ``db_path`` and ``storage_backend`` are embedded-backend constructor
    holdovers slated for retirement with that backend; until the
    signature drops them, tests pass the Postgres backend explicitly and
    a brain-root path that must never be created (MNT-001 asserts it).
    """
    return MaintenanceService(
        vault_id=config.vault.id,
        db_path=Path(config.vault.brain_root) / "graph.db",
        graph_store=graph_store,
        config=config,
        registry_service=registry_service,
        content_store=content_store if content_store is not None else StubContentStore(),
        vault_dir=vault_dir,
        storage_backend="postgres",
    )


# ---------------------------------------------------------------------------
# MaintenanceService.migrate_vault: schema no-op + tier3 scan (CAS-ADR-042)
# ---------------------------------------------------------------------------


@pytest.fixture
def unique_keys_config(tmp_vault_dir):
    """Vault config declaring ``ticket.ticket_id`` (and
    ``failure_record.failure_id``) as tier3 unique keys."""
    return VaultConfig.model_validate(_config_dict_with_unique_keys(tmp_vault_dir))


class _RecordingRegistryService:
    """Registry stand-in whose ``reload`` records each call instead of
    rebuilding services, so a test can assert reload never fires."""

    def __init__(self) -> None:
        self.reload_calls: list[str] = []

    async def reload(self, vault_id: str, config: VaultConfig) -> None:
        self.reload_calls.append(vault_id)


async def test_migrate_vault_is_schema_noop_that_still_runs_tier3_scan(
    graph_store, unique_keys_config, stub_content_store
):
    """MNT-001: ``migrate_vault`` reports no schema work, touches no local
    file, and still surfaces tier3 collisions through the live graph store.

    Two traps. A lingering SQLite detect path would ``sqlite3.connect`` the
    brain-root ``graph.db`` — silently creating a stray file beside the real
    (Postgres) store — so the file's continued absence is asserted. And a fix
    that short-circuited the whole method rather than just the schema step
    would drop the tier3 scan, missing the seeded cross-chain collision.
    """
    colliding_a = _make_ticket_doc("doc-a", "T-0001")
    colliding_b = _make_ticket_doc("doc-b", "T-0001")
    await graph_store.insert_document(colliding_a)
    await graph_store.insert_document(colliding_b)

    db_path = Path(unique_keys_config.vault.brain_root) / "graph.db"
    assert not db_path.exists()
    maintenance = _maintenance_for(
        graph_store, unique_keys_config, content_store=stub_content_store
    )

    report = await maintenance.migrate_vault()

    assert isinstance(report, MigrationReport)
    assert report.vault_id == unique_keys_config.vault.id
    assert report.columns_added == []
    assert report.backfills_applied == []
    ticket_collisions = [c for c in report.tier3_uniqueness_collisions if c.doc_type == "ticket"]
    assert len(ticket_collisions) == 1
    assert ticket_collisions[0].field == "ticket_id"
    assert ticket_collisions[0].value == "T-0001"
    assert set(ticket_collisions[0].document_ids) == {colliding_a.id, colliding_b.id}
    # The colliding declaration must not activate.
    activated = {(a.doc_type, a.field) for a in report.tier3_uniqueness_activations}
    assert ("ticket", "ticket_id") not in activated
    # No stray SQLite file: the detect path's first act (sqlite3.connect)
    # would create this file, so its absence proves the path never ran.
    assert not db_path.exists()


async def test_migrate_vault_noop_is_idempotent_and_never_reloads_registry(
    graph_store, unique_keys_config, stub_content_store, pg_pool, pg_schema
):
    """MNT-002: on a clean portfolio, two ``migrate_vault`` calls return
    identical empty schema reports, activate the declared tier3 index in
    Postgres, and never trigger a registry reload.

    Trap: the embedded backend reloaded the registry whenever pending schema
    work was detected. A leftover reload-on-pending-work path — or a reload
    wired unconditionally into the no-op branch — would record a call on the
    stub registry, tearing down live services for nothing.
    """
    await graph_store.insert_document(_make_ticket_doc("doc-a", "T-0001"))
    await graph_store.insert_document(_make_ticket_doc("doc-b", "T-0002"))

    registry_service = _RecordingRegistryService()
    maintenance = _maintenance_for(
        graph_store,
        unique_keys_config,
        content_store=stub_content_store,
        registry_service=registry_service,
    )

    first = await maintenance.migrate_vault()
    second = await maintenance.migrate_vault()

    for report in (first, second):
        assert report.columns_added == []
        assert report.backfills_applied == []
        assert report.tier3_uniqueness_collisions == []
        activated = {(a.doc_type, a.field) for a in report.tier3_uniqueness_activations}
        assert ("ticket", "ticket_id") in activated

    # The activation is durable in the Postgres catalog under the vault's
    # schema, named by the canonical helper.
    index_name = tier3_unique_index_name("ticket", "ticket_id")
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
            (pg_schema, index_name),
        )
        assert await cur.fetchone() is not None, f"{index_name} missing from pg_indexes"

    assert registry_service.reload_calls == []


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
    return "sha256:" + hashlib.sha256(f"drift-test:{suffix}".encode()).hexdigest()


async def test_detect_drift_multi_basket(graph_store, minimal_config, stub_content_store):
    """T-DD-multi: one fixture, four edges, three expected baskets.

    - A: hash matches current head → absent from report.
    - B: hash differs from head → content_drift.
    - C: hash matches head but synced_from_version != head_id →
      chain_advanced_no_content_change.
    - D: both fields NULL → recorded_null.
    """
    gs = graph_store
    maintenance = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

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
    assert report.vault_id == minimal_config.vault.id
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


async def test_detect_drift_nonlinear_chain(graph_store, minimal_config, stub_content_store):
    """T-DD-nonlinear: target with a forked chain (two heads) is reported
    with staleness_basis=chain_nonlinear; head fields are null and
    competing_head_count carries the fork width."""
    gs = graph_store
    maintenance = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

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


async def test_detect_drift_version_only(graph_store, minimal_config, stub_content_store):
    """T-DD-version-only: edges with synced_from_version set but
    synced_from_content_hash NULL. Three sub-cases — current (recorded
    == head), chain_advanced (recorded != head, recorded.hash == head.hash),
    content_drift (recorded != head, recorded.hash != head.hash)."""
    gs = graph_store
    maintenance = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

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


def _write_source(config: VaultConfig, source_path: str, content: bytes) -> Path:
    """Write a real file at storage_root/source_path (mkdir parents)."""
    p = Path(config.vault.storage_root) / source_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


async def test_verify_source_files_clean_vault_returns_empty(
    graph_store, minimal_config, stub_content_store
):
    """Happy path: every document's source file present → empty report,
    all healthy."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    for i in range(3):
        sp = f"imports/deadbeef_d{i}.md"
        body = f"body {i}".encode()
        _write_source(minimal_config, sp, body)
        await gs.insert_document(_src_doc(f"deadbeef_d{i}", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.vault_id == minimal_config.vault.id
    assert report.total_documents_checked == 3
    assert report.check_hashes is False
    assert report.entries == []
    assert report.summary == {"healthy": 3, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_flags_missing_file(
    graph_store, minimal_config, stub_content_store
):
    """The precipitating incident: one document's backing file is absent.
    It is the only entry, classified `missing`, and the present docs stay
    healthy."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    for did, present in (("aaaaaaaa_a", True), ("bbbbbbbb_b", False), ("cccccccc_c", True)):
        sp = f"imports/{did}.md"
        body = f"{did} body".encode()
        if present:
            _write_source(minimal_config, sp, body)
        await gs.insert_document(_src_doc(did, _sha256_of(body), source_path=sp))

    # Positive control: the present files exist; the flagged one does not.
    root = Path(minimal_config.vault.storage_root)
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


async def test_verify_source_files_surfaces_archived_missing(
    graph_store, minimal_config, stub_content_store
):
    """Scope is all lifecycle states: an archived record with a missing
    file is surfaced (the incident's residual was an archived version)."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

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


async def test_verify_source_files_existence_mode_ignores_hash_drift(
    graph_store, minimal_config, stub_content_store
):
    """check_hashes=False never reads content: a present file whose bytes
    do not match the recorded hash is NOT flagged."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_h.md"
    _write_source(minimal_config, sp, b"actual content")
    await gs.insert_document(
        _src_doc("deadbeef_h", _sha256_of(b"different content"), source_path=sp)
    )

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.entries == []
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_hash_mode_flags_mismatch(
    graph_store, minimal_config, stub_content_store
):
    """check_hashes=True: a present file whose bytes diverge from the
    recorded hash surfaces as `hash_mismatch` with the observed hash."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_h.md"
    _write_source(minimal_config, sp, b"actual content")
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


async def test_verify_source_files_hash_mode_clean_when_matching(
    graph_store, minimal_config, stub_content_store
):
    """check_hashes=True with a matching on-disk file → no entry. Guards
    against the mismatch path firing on every file."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_h.md"
    body = b"matching content"
    _write_source(minimal_config, sp, body)
    await gs.insert_document(_src_doc("deadbeef_h", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.entries == []
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0}


async def test_verify_source_files_hash_mode_missing_stays_missing(
    graph_store, minimal_config, stub_content_store
):
    """A missing file under check_hashes=True classifies as `missing` (not
    a hash error) with a null observed hash."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

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


_OPTIMIZE_SNAPSHOT = ContentStoreOptimizeSnapshot(
    pre_bytes=10_000,
    post_bytes=4_000,
    pre_versions=9,
    post_versions=1,
    pre_fragments=8,
    post_fragments=2,
    pre_small_fragments=5,
    post_small_fragments=0,
)


class _SnapshotContentStore(StubContentStore):
    """Content store whose ``optimize`` returns a scripted snapshot and
    records the thresholds it was invoked with.

    The service-level contract under test is report shaping and audit
    logging around the ContentStore port; real on-disk reclamation is
    covered by the content-store test modules.
    """

    def __init__(self, snapshot: ContentStoreOptimizeSnapshot | None = None) -> None:
        super().__init__()
        self.snapshot = snapshot or _OPTIMIZE_SNAPSHOT
        self.optimize_calls: list[timedelta] = []

    async def optimize(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        self.optimize_calls.append(cleanup_older_than)
        return ContentStoreOptimizeSnapshot(**self.snapshot)


def _optimize_maintenance(
    config: VaultConfig,
    vault_dir: Path | None,
    store: _SnapshotContentStore,
) -> MaintenanceService:
    """MaintenanceService for the optimize tests: the graph store is never
    consulted by ``optimize_content_store``, so a stub suffices."""
    return _maintenance_for(StubGraphStore(), config, content_store=store, vault_dir=vault_dir)


async def test_optimize_content_store_shapes_report_and_writes_audit(minimal_config, tmp_path):
    """The report is built from the store's snapshot — bytes_reclaimed is the
    pre/post delta, the threshold echoes back, and the timestamps bracket the
    call — and exactly one JSONL audit line lands that matches the report.

    Guards writer-side field agreement: renaming a snapshot key or dropping
    an audit field breaks the line-by-line comparison below.
    """
    store = _SnapshotContentStore()
    maintenance = _optimize_maintenance(minimal_config, tmp_path, store)

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    assert isinstance(report, OptimizeContentStoreReport)
    assert report.vault_id == minimal_config.vault.id
    assert report.cleanup_older_than_days == 0
    assert report.pre_bytes == 10_000
    assert report.post_bytes == 4_000
    assert report.bytes_reclaimed == 6_000
    assert report.pre_versions == 9
    assert report.post_versions == 1
    assert report.finished_at >= report.started_at
    # The threshold reached the store as a timedelta, not a default.
    assert store.optimize_calls == [timedelta(days=0)]

    # Audit log: one JSONL line that round-trips and matches the report.
    audit_path = tmp_path / ".maintenance_log.jsonl"
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


@pytest.mark.parametrize("cleanup_days", [0, 7, 999999])
async def test_optimize_content_store_threshold_passes_through(
    minimal_config, tmp_path, cleanup_days: int
):
    """cleanup_older_than_days is honored end-to-end: the store receives the
    caller's threshold as a timedelta and the report echoes it.

    Trap: a code path that ignores the parameter and passes the substrate
    default would satisfy a shape-only assertion; recording the store-side
    argument discriminates it.
    """
    store = _SnapshotContentStore()
    maintenance = _optimize_maintenance(minimal_config, tmp_path, store)

    report = await maintenance.optimize_content_store(cleanup_older_than_days=cleanup_days)

    assert report.cleanup_older_than_days == cleanup_days
    assert store.optimize_calls == [timedelta(days=cleanup_days)]


async def test_optimize_content_store_bytes_reclaimed_floors_at_zero(minimal_config, tmp_path):
    """A snapshot where compaction work grew the store (post > pre) reports
    bytes_reclaimed == 0, never a negative value."""
    grown = ContentStoreOptimizeSnapshot(**{**_OPTIMIZE_SNAPSHOT, "post_bytes": 12_000})
    store = _SnapshotContentStore(grown)
    maintenance = _optimize_maintenance(minimal_config, tmp_path, store)

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    assert report.bytes_reclaimed == 0


async def test_optimize_content_store_rejects_negative_days(minimal_config, tmp_path):
    """Negative cleanup_older_than_days is a service-level
    precondition violation -- the validation must live on the service
    method itself, not only on the Pydantic request model, because the
    MCP-tool surface calls the service directly.
    """
    store = _SnapshotContentStore()
    maintenance = _optimize_maintenance(minimal_config, tmp_path, store)

    with pytest.raises(ValueError, match="cleanup_older_than_days must be >= 0"):
        await maintenance.optimize_content_store(cleanup_older_than_days=-1)

    # The store must not be reached and the audit log must NOT be appended
    # on a rejected call.
    assert store.optimize_calls == []
    audit_path = tmp_path / ".maintenance_log.jsonl"
    assert not audit_path.exists(), "audit log should not be written on a rejected call"


async def test_read_last_optimize_summary_none_when_no_log(tmp_path):
    """No maintenance log on disk → reader returns None (never-optimized vault)."""
    from sage.services.maintenance_log import read_last_optimize_summary

    assert read_last_optimize_summary(tmp_path) is None


async def test_read_last_optimize_summary_after_optimize(minimal_config, tmp_path):
    """Round-trip: an optimize writes the audit record and the reader
    returns a summary whose fields match that record.

    Guards writer/reader key agreement -- renaming a key in either half
    breaks this test.
    """
    from sage.services.maintenance_log import read_last_optimize_summary

    store = _SnapshotContentStore()
    maintenance = _optimize_maintenance(minimal_config, tmp_path, store)
    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    summary = read_last_optimize_summary(tmp_path)
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


# ---------------------------------------------------------------------------
# get_stats surfacing of content-store health measures
# ---------------------------------------------------------------------------


class _CountingContentStore(StubContentStore):
    """Content store reporting distinctive nonzero health counts, so a
    surfacing test can tell a real passthrough from a constant."""

    async def count_retained_versions(self) -> int:
        return 7

    async def count_small_fragments(self) -> int:
        return 3


async def test_get_stats_surfaces_content_store_health_counts(graph_store, minimal_config):
    """get_stats exposes the retained-version and small-fragment counts to
    the dashboard.

    Regression guard: the values the HTTP stats endpoint returns must equal
    the content store's own read-only measures, not constants (the stub
    baseline is 0 for both, so a hardcoded passthrough fails).
    """
    from sage.services.vault_config import VaultConfigService

    service = VaultConfigService(graph_store, _CountingContentStore(), minimal_config, None)

    stats = await service.get_stats()

    assert stats.content_store_version_count == 7
    assert stats.content_store_small_fragment_count == 3


async def test_get_stats_surfaces_last_optimize(graph_store, minimal_config):
    """get_stats surfaces the last-optimize summary; None before any optimize.

    Writer and reader must agree on the vault directory: the maintenance
    service is built WITHOUT a vault_dir override so its audit record lands
    where get_stats reads (``config_path_for_vault(vault_id).parent``,
    redirected into tmp space by the test harness).
    """
    from sage.services.vault_config import VaultConfigService

    store = _SnapshotContentStore()
    maintenance = _maintenance_for(StubGraphStore(), minimal_config, content_store=store)
    stats_service = VaultConfigService(graph_store, store, minimal_config, None)

    stats_before = await stats_service.get_stats()
    assert stats_before.last_optimize is None

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    stats_after = await stats_service.get_stats()
    assert stats_after.last_optimize is not None
    assert stats_after.last_optimize.bytes_reclaimed == report.bytes_reclaimed
