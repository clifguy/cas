"""Unit tests for MaintenanceService (CAS-ADR-029).

Exercises the maintenance surface over the Postgres storage binding
(CAS-ADR-042):

1. ``migrate_vault`` is a schema no-op — the schema is provisioned
   externally, so the report carries no column work, no local file is
   touched, and the registry is never reloaded — while the
   backend-agnostic tier3-uniqueness scan (CAS-ADR-031) and the one
   standing data backfill both still run on every call. The backfill
   names itself in ``backfills_applied`` only when it changed rows, so
   the no-op reports here stay empty.
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
from sage.api.errors import (
    RestoreProvenanceMismatchError,
    RestoreSourceNotAbsoluteError,
    RestoreTargetUnresolvedError,
    SourceFileNotFoundError,
    VaultSourcePathRefusedError,
)
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType, StalenessBasis
from sage.models.schemas import (
    Document,
    DriftReport,
    Edge,
    MigrationReport,
    OptimizeContentStoreReport,
)
from sage.services.maintenance import BACKFILL_STALE_PIPELINE_ERROR, MaintenanceService
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
    """Build a MaintenanceService over the given store pair."""
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=registry_service,
        content_store=content_store if content_store is not None else StubContentStore(),
        vault_dir=vault_dir,
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


def _recovered_doc(short_name: str, ticket_id: str, status: PipelineStatus) -> Document:
    """A ticket document sitting at ``status`` with a stale pipeline_error."""
    return _make_ticket_doc(short_name, ticket_id).model_copy(
        update={
            "pipeline_status": status,
            "pipeline_error": "abstraction failed after 3 attempts; last error: stale detail",
        }
    )


async def test_migrate_vault_backfills_stale_pipeline_errors(
    graph_store, minimal_config, stub_content_store
):
    """MNT-003: the migration clears errors left on already-recovered documents.

    ``pipeline_error`` predates the rule that a successful terminal
    ``pipeline_status`` clears it, so documents repaired before the rule
    existed still describe a failure that no longer holds. The migration is
    the operator-facing surface that repairs them.

    Trap: a document still at ``failed`` describes a live failure and must
    keep its message; clearing it would erase real state under cover of a
    cleanup.
    """
    recovered = _recovered_doc("doc-a", "T-0001", PipelineStatus.ABSTRACTION_COMPLETE)
    skipped = _recovered_doc("doc-b", "T-0002", PipelineStatus.ABSTRACTION_SKIPPED)
    still_failed = _recovered_doc("doc-c", "T-0003", PipelineStatus.FAILED)
    for doc in (recovered, skipped, still_failed):
        await graph_store.insert_document(doc)

    maintenance = _maintenance_for(graph_store, minimal_config, content_store=stub_content_store)

    report = await maintenance.migrate_vault()

    assert report.backfills_applied == [BACKFILL_STALE_PIPELINE_ERROR]
    assert (await graph_store.get_document(recovered.id)).pipeline_error is None
    assert (await graph_store.get_document(skipped.id)).pipeline_error is None
    assert (await graph_store.get_document(still_failed.id)).pipeline_error is not None


async def test_migrate_vault_reports_no_backfill_when_clean(
    graph_store, minimal_config, stub_content_store
):
    """MNT-004: a vault with nothing to repair reports an empty backfill list.

    Anti-coincidental-pass: this is the half MNT-003 cannot catch. An
    unconditional append passes MNT-003 and turns every migration into a
    report of work that never happened, which is exactly the false-positive
    the backfill exists to remove.
    """
    clean = _make_ticket_doc("doc-a", "T-0001")
    assert clean.pipeline_error is None
    await graph_store.insert_document(clean)

    maintenance = _maintenance_for(graph_store, minimal_config, content_store=stub_content_store)

    first = await maintenance.migrate_vault()
    assert first.backfills_applied == []

    # And a re-call after a genuine repair reports nothing further.
    await graph_store.insert_document(
        _recovered_doc("doc-b", "T-0002", PipelineStatus.ABSTRACTION_COMPLETE)
    )
    assert (await maintenance.migrate_vault()).backfills_applied == [BACKFILL_STALE_PIPELINE_ERROR]
    assert (await maintenance.migrate_vault()).backfills_applied == []


async def test_stub_graph_store_clear_pipeline_error_matches_port_contract(minimal_config):
    """The stub honors the same predicate as the durable store.

    The stub stands in for the graph store across the service tests, so a
    permissive stub would let a service-level test pass over behavior the real
    store refuses. Same four-row separation as the Postgres case.
    """
    store = StubGraphStore()
    recovered = _recovered_doc("doc-a", "T-0001", PipelineStatus.ABSTRACTION_COMPLETE)
    skipped = _recovered_doc("doc-b", "T-0002", PipelineStatus.ABSTRACTION_SKIPPED)
    still_failed = _recovered_doc("doc-c", "T-0003", PipelineStatus.FAILED)
    already_clean = _make_ticket_doc("doc-d", "T-0004")
    for doc in (recovered, skipped, still_failed, already_clean):
        await store.insert_document(doc)

    statuses = [
        PipelineStatus.ABSTRACTION_COMPLETE.value,
        PipelineStatus.ABSTRACTION_SKIPPED.value,
    ]

    assert await store.clear_pipeline_error_for_statuses(statuses) == 2
    assert (await store.get_document(recovered.id)).pipeline_error is None
    assert (await store.get_document(skipped.id)).pipeline_error is None
    assert (await store.get_document(still_failed.id)).pipeline_error is not None
    assert await store.clear_pipeline_error_for_statuses(statuses) == 0
    assert await store.clear_pipeline_error_for_statuses([]) == 0


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


async def test_detect_drift_ignores_hash_spelling(graph_store, minimal_config, stub_content_store):
    """A recorded hash differing from the head only in case is not drift.

    Drift reads `synced_from_content_hash` from a raw edge row, which never
    crosses the `Sha256Str` alias, so a row written before the alias may carry
    a non-canonical spelling. Compared raw, such a row reports content_drift
    against a hash it actually equals -- and because `DriftEntry` canonicalizes
    both fields as it is built, the entry would render two identical hashes as
    the evidence of the difference.

    Two dimensions vary independently, and a rival exists for each. Both sides
    are read from raw rows, so a fix applied to only one of them still passes a
    test that varies only the other. And a fix that lowercases without
    normalizing the algorithm prefix handles every case-only difference while
    still failing a bare-hex row -- the form `hexdigest()` produces, and so the
    likeliest spelling for a row predating the alias.

    All the non-canonical spellings are applied after construction on purpose:
    `Edge` and `Document` both normalize during validation, so a fixture passing
    them to a constructor could not express these legacy rows at all.
    """
    gs = graph_store
    maintenance = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    # Letter-only digests: digits have no case, so a digit digest could not
    # express an uppercase spelling and the test would pass vacuously.
    head_hash = _hash("e")
    await gs.insert_document(_drift_doc("cafebabe_t2", head_hash, "v2"))
    await gs.insert_document(_drift_doc("aaaaaaaa_a", _hash("1")))
    await gs.insert_document(_drift_doc("bbbbbbbb_b", _hash("2")))

    # A second target whose *stored* hash is the non-canonical one, so the
    # head side of the comparison is the side that varies.
    other_head_hash = _hash("d")
    other_target = _drift_doc("deadbeef_t3", other_head_hash, "v1")
    other_target.source_content_hash = "sha256:" + other_head_hash.removeprefix("sha256:").upper()
    assert other_target.source_content_hash != other_head_hash
    await gs.insert_document(other_target)
    await gs.insert_document(_drift_doc("cccccccc_c", _hash("3")))
    await gs.insert_document(_drift_doc("dddddddd_d", _hash("4")))

    spelling_only = _drift_edge(
        "66666666-6666-4666-8666-666666666666",
        source_id="aaaaaaaa_a",
        target_id="cafebabe_t2",
        synced_from_version="cafebabe_t2",
        synced_from_content_hash=head_hash,
    )
    # Simulate a row predating the alias: same digest, uppercased hex.
    spelling_only.synced_from_content_hash = "sha256:" + head_hash.removeprefix("sha256:").upper()
    assert spelling_only.synced_from_content_hash != head_hash
    await gs.insert_edge(spelling_only)

    # The mirror case: a canonical recorded hash against a non-canonical stored
    # head. Canonicalizing only the recorded side leaves this one comparing
    # lowercase against uppercase, so it reports drift and this test goes red.
    head_side_only = _drift_edge(
        "88888888-8888-4888-8888-888888888888",
        source_id="cccccccc_c",
        target_id="deadbeef_t3",
        synced_from_version="deadbeef_t3",
        synced_from_content_hash=other_head_hash,
    )
    await gs.insert_edge(head_side_only)

    # The prefix dimension: a bare-hex recorded hash against a prefixed head.
    # Lowercasing both sides without normalizing the prefix leaves this one
    # comparing 64 characters against 71, so it reports drift and this test
    # goes red.
    prefix_only = _drift_edge(
        "99999999-9999-4999-8999-999999999999",
        source_id="dddddddd_d",
        target_id="cafebabe_t2",
        synced_from_version="cafebabe_t2",
        synced_from_content_hash=head_hash,
    )
    prefix_only.synced_from_content_hash = head_hash.removeprefix("sha256:")
    assert not prefix_only.synced_from_content_hash.startswith("sha256:")
    await gs.insert_edge(prefix_only)

    # Positive control: a genuinely different hash on the same target. Absence
    # alone cannot distinguish "judged current" from "never evaluated" -- this
    # edge proves the classifier reached this target and still reports drift.
    genuinely_drifted = _drift_edge(
        "77777777-7777-4777-8777-777777777777",
        source_id="bbbbbbbb_b",
        target_id="cafebabe_t2",
        synced_from_version="cafebabe_t2",
        synced_from_content_hash=_hash("f"),
    )
    await gs.insert_edge(genuinely_drifted)

    report = await maintenance.detect_drift()

    edge_ids = {e.edge_id for e in report.entries}
    assert "77777777-7777-4777-8777-777777777777" in edge_ids, (
        "positive control: a genuinely different hash must still report drift"
    )
    assert "66666666-6666-4666-8666-666666666666" not in edge_ids, (
        "a non-canonical recorded hash must not read as content drift"
    )
    assert "88888888-8888-4888-8888-888888888888" not in edge_ids, (
        "a non-canonical stored head hash must not read as content drift either "
        "-- canonicalizing only the recorded side would fail here"
    )
    assert "99999999-9999-4999-8999-999999999999" not in edge_ids, (
        "a bare-hex recorded hash must not read as content drift -- lowercasing "
        "without normalizing the algorithm prefix would fail here"
    )


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
    stored_content_hash: str | None = None,
) -> Document:
    """A Document for source-file-audit tests, parameterized on the fields
    the audit reads: source_path, lifecycle_status, source_content_hash,
    stored_content_hash.

    ``stored_content_hash`` defaults to None, which is the shape of a document
    ingested before the two digests were recorded separately -- so every
    pre-existing test in this module keeps exercising the fallback comparator.
    """
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Source test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=source_path,
        lifecycle_status=lifecycle_status,
        source_content_hash=content_hash,
        stored_content_hash=stored_content_hash,
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
    assert report.summary == {"healthy": 3, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


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
    assert report.summary == {"healthy": 2, "missing": 1, "hash_mismatch": 0, "symlinked": 0}
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
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


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
    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 1, "symlinked": 0}
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
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


async def test_verify_source_files_healthy_when_stored_copy_diverges_from_provenance(
    graph_store, minimal_config, stub_content_store
):
    """A document whose retained copy is not byte-identical to what the caller
    delivered audits healthy while that copy is intact.

    The shape a binding that rewrites its copy at rest produces: provenance
    records the delivered bytes, the stored digest records the retained ones, and
    the audit re-reads the retained ones.

    Anti-coincidental-pass: the two recorded digests genuinely differ (asserted),
    so an audit still comparing against ``source_content_hash`` reports this
    document as corrupt. A fixture where they happened to agree would pass under
    either comparator and prove nothing.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_office.md"
    delivered = b"the bytes the caller handed over"
    retained = b"the bytes the store chose to keep"
    _write_source(minimal_config, sp, retained)
    doc = _src_doc(
        "deadbeef_office",
        _sha256_of(delivered),
        source_path=sp,
        stored_content_hash=_sha256_of(retained),
    )
    assert doc.source_content_hash != doc.stored_content_hash
    await gs.insert_document(doc)

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.entries == []
    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


async def test_verify_source_files_detects_corruption_of_a_diverged_stored_copy(
    graph_store, minimal_config, stub_content_store
):
    """Corruption of the retained copy is still caught for a document whose
    provenance and stored digests differ, and the report names the digest the
    audit actually compared against.

    Anti-coincidental-pass: the reported ``expected_content_hash`` is asserted to
    be the *stored* digest and explicitly not the provenance one. Without that,
    an implementation could compare correctly and then report a hash it never
    used -- leaving an operator to chase a mismatch against the wrong baseline.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_office.md"
    delivered = b"the bytes the caller handed over"
    retained = b"the bytes the store chose to keep"
    corrupted = b"bytes nobody asked for"
    _write_source(minimal_config, sp, corrupted)
    await gs.insert_document(
        _src_doc(
            "deadbeef_office",
            _sha256_of(delivered),
            source_path=sp,
            stored_content_hash=_sha256_of(retained),
        )
    )

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 1, "symlinked": 0}
    entry = report.entries[0]
    assert entry.integrity_status == "hash_mismatch"
    assert entry.expected_content_hash == _sha256_of(retained)
    assert entry.expected_content_hash != _sha256_of(delivered)
    assert entry.observed_content_hash == _sha256_of(corrupted)


async def test_verify_source_files_falls_back_for_a_document_with_no_stored_hash(
    graph_store, minimal_config, stub_content_store
):
    """A document ingested before the two digests were recorded separately --
    null ``stored_content_hash`` -- audits against ``source_content_hash``, in
    both directions.

    Pins the policy for those records: corruption detection is unaffected by
    their age. Anti-coincidental-pass: both directions are asserted in one test,
    so a comparator that reported every null-stored-hash document as healthy
    (skipping the check rather than falling back) fails on the second half.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    intact = b"legacy body"
    _write_source(minimal_config, "imports/legacy_ok.md", intact)
    await gs.insert_document(
        _src_doc("aaaaaaaa_ok", _sha256_of(intact), source_path="imports/legacy_ok.md")
    )
    _write_source(minimal_config, "imports/legacy_bad.md", b"body as it is now")
    await gs.insert_document(
        _src_doc(
            "bbbbbbbb_bad",
            _sha256_of(b"body as it was ingested"),
            source_path="imports/legacy_bad.md",
        )
    )

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 1, "symlinked": 0}
    entry = report.entries[0]
    assert entry.document_id == "bbbbbbbb_bad"
    assert entry.expected_content_hash == _sha256_of(b"body as it was ingested")


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

    assert report.summary == {"healthy": 0, "missing": 1, "hash_mismatch": 0, "symlinked": 0}
    entry = report.entries[0]
    assert entry.integrity_status == "missing"
    assert entry.observed_content_hash is None


def _link_source(config: VaultConfig, source_path: str, target: Path) -> Path:
    """Replace ``source_path`` with a symlink to ``target``, as something other
    than SAGE would."""
    p = Path(config.vault.storage_root) / source_path
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() or p.is_symlink():
        p.unlink()
    p.symlink_to(target)
    return p


def test_retained_copy_observation_symlinked_is_never_intact():
    """A linked path is not intact even when a digest in hand matches.

    ``intact`` is where the rule lives, and it has to hold for any observation
    rather than only for the ones the helper happens to build. Today the helper
    reports a linked path with a null digest, which would make ``intact`` false
    on the digest alone -- so the two facts coincide and the rule looks
    unnecessary. They are different facts: the null digest says the audit did
    not read through the link, while this says being a link disqualifies the
    copy however it hashes. A later change that let a linked observation carry a
    digest -- to report what the link resolves to, say -- would silently restore
    ``already_intact`` for exactly the state this change exists to surface.

    Anti-coincidental-pass: the digest supplied *matches* the expected one, so
    every part of the predicate except the link term votes intact. An
    implementation resting on the null digest fails here and nowhere else.
    """
    from sage.services.maintenance import _RetainedCopyObservation

    digest = _sha256_of(b"identical either way")
    linked = _RetainedCopyObservation(
        present=True, observed_hash=digest, expected_hash=digest, symlinked=True
    )
    ordinary = _RetainedCopyObservation(present=True, observed_hash=digest, expected_hash=digest)

    assert linked.intact is False
    assert ordinary.intact is True, "the link term must not disqualify an ordinary copy"


async def test_observe_retained_copy_keeps_presence_behind_a_link(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A linked path still records whether bytes resolve behind it.

    Both are linked and neither is intact, but they are not the same fault and
    do not call for the same repair: a dangling link means the content is gone
    and must be re-delivered, while a link onto a real file means the bytes are
    already there and only a real copy has to be put back at the recorded path.
    Collapsing both to absent throws away the one fact that separates them --
    and it is the same fact the classification ordering rests on, since it is
    presence that would otherwise call a dangling link `missing`.

    Anti-coincidental-pass: the two observations are asserted to *differ* in
    ``present``, so an implementation hardcoding either constant fails on one
    of them. ``symlinked`` and ``intact`` are asserted equal across the pair,
    so the difference cannot be smuggled in by classifying them apart.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)
    store = maint._vault_source_store()
    storage_root = maint._storage_root()

    body = b"bytes that exist"
    target = tmp_path / "elsewhere" / "real.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)

    _link_source(minimal_config, "imports/resolving.md", target)
    resolving_doc = _src_doc("aaaaaaaa_res", _sha256_of(body), source_path="imports/resolving.md")
    _link_source(minimal_config, "imports/dangling.md", tmp_path / "never_written.md")
    dangling_doc = _src_doc("bbbbbbbb_dng", _sha256_of(body), source_path="imports/dangling.md")

    resolving = maint._observe_retained_copy(resolving_doc, storage_root, store)
    dangling = maint._observe_retained_copy(dangling_doc, storage_root, store)

    assert resolving.symlinked is True and dangling.symlinked is True
    assert resolving.intact is False and dangling.intact is False
    assert resolving.present is True, "bytes resolve behind this link; the copy is not gone"
    assert dangling.present is False, "nothing resolves behind this link"
    assert resolving.observed_hash is None, "neither link is read through"
    assert dangling.observed_hash is None


async def test_verify_source_files_reports_a_symlinked_path_holding_correct_bytes(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A recorded path that *is* a link is reported as such, not read through.

    The scenario: something other than SAGE swaps the retained copy for a link
    to a file holding the expected bytes. The write side already refuses such a
    path, so the record names a location the repair primitive will not accept --
    and until now nothing said so.

    Anti-coincidental-pass: the link's target holds exactly the recorded bytes,
    so every rival that reads through the link -- which is what the audit did --
    finds the document healthy and reports nothing at all. A link to *drifted*
    content would surface as `hash_mismatch` without the new observation ever
    being consulted, which is why the bytes behind the link are the originals.
    The null observed hash is asserted separately, so an implementation that
    classified correctly but still hashed through the link fails.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_link.md"
    body = b"the bytes the record describes"
    target = tmp_path / "elsewhere" / "real.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _link_source(minimal_config, sp, target)
    await gs.insert_document(_src_doc("deadbeef_link", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 0, "symlinked": 1}
    entry = report.entries[0]
    assert entry.document_id == "deadbeef_link"
    assert entry.integrity_status == "symlinked"
    assert entry.observed_content_hash is None, "the audit reports the link, it does not read it"
    assert entry.expected_content_hash == _sha256_of(body)


async def test_verify_source_files_reports_a_symlinked_path_in_existence_mode(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """The link is reported with ``check_hashes=False`` too.

    Asking whether a path is a link is an ``lstat``, not a content read, so the
    existence-only mode carries no new cost and has no reason to withhold the
    finding.

    Anti-coincidental-pass: the hash branch is off entirely here, so an
    implementation that reached the classification through the digest
    comparison produces no entry at all rather than a differently-named one.
    That is what proves the status is a distinct observation and not a hash
    finding wearing a new label.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_elink.md"
    body = b"existence mode body"
    target = tmp_path / "elsewhere" / "real.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _link_source(minimal_config, sp, target)
    await gs.insert_document(_src_doc("deadbeef_elink", _sha256_of(body), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=False)

    assert report.check_hashes is False
    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 0, "symlinked": 1}
    assert report.entries[0].integrity_status == "symlinked"
    assert report.entries[0].observed_content_hash is None


async def test_verify_source_files_reports_a_dangling_link_as_symlinked_not_missing(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A link to nothing is still a link at the recorded path.

    Classifying it `missing` would send an operator to a repair that then
    refuses: the restore writes through the port, and the binding will not write
    at a linked destination however absent its target. Reporting `symlinked`
    makes the audit predict the refusal the repair will actually give.

    Anti-coincidental-pass: ``source_exists`` is False for a dangling link, so
    any implementation that asks about presence before asking about the link
    classifies this `missing`. The zero `missing` count is what excludes that
    rival; asserting only the `symlinked` status would leave a double-count
    implementation green.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_dangle.md"
    _link_source(minimal_config, sp, tmp_path / "never_written.md")
    await gs.insert_document(_src_doc("deadbeef_dangle", _sha256_of(b"x"), source_path=sp))

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 0, "missing": 0, "hash_mismatch": 0, "symlinked": 1}
    assert report.entries[0].integrity_status == "symlinked"


async def test_verify_source_files_summary_counts_all_four_states(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """The four summary counts partition the documents walked.

    Anti-coincidental-pass: the vault is mixed, so a summary that hardcodes
    ``"symlinked": 0``, or that files the linked row into an existing bucket,
    fails the equality -- neither of which a single-document fixture could
    catch. The sum is asserted against the total independently, so a
    classification that emitted an entry without counting it (or counted it
    twice) fails even if the individual keys look plausible.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    healthy_body = b"present and correct"
    _write_source(minimal_config, "imports/mixed_ok.md", healthy_body)
    await gs.insert_document(
        _src_doc("aaaaaaaa_ok", _sha256_of(healthy_body), source_path="imports/mixed_ok.md")
    )

    linked_body = b"behind the link"
    target = tmp_path / "elsewhere" / "linked.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(linked_body)
    _link_source(minimal_config, "imports/mixed_link.md", target)
    await gs.insert_document(
        _src_doc("bbbbbbbb_ln", _sha256_of(linked_body), source_path="imports/mixed_link.md")
    )

    await gs.insert_document(
        _src_doc("cccccccc_gone", _sha256_of(b"x"), source_path="imports/mixed_gone.md")
    )

    report = await maint.verify_vault_source_files(check_hashes=True)

    assert report.summary == {"healthy": 1, "missing": 1, "hash_mismatch": 0, "symlinked": 1}
    assert sum(report.summary.values()) == report.total_documents_checked
    statuses = {e.document_id: e.integrity_status for e in report.entries}
    assert statuses == {"bbbbbbbb_ln": "symlinked", "cccccccc_gone": "missing"}


# ---------------------------------------------------------------------------
# MaintenanceService.restore_vault_source_file
# ---------------------------------------------------------------------------


def _delivered(tmp_path: Path, name: str, body: bytes) -> str:
    """A caller-side file holding the bytes to restore, outside the vault."""
    p = tmp_path / "operator_copy" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return str(p)


async def test_restore_source_file_repairs_a_drifted_copy(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A retained copy that changed out of band is repaired by writing the
    original bytes back, and the audit goes clean afterwards.

    Anti-coincidental-pass: the rival this must exclude is the one that leaves
    the audit equally clean while fixing nothing -- refreshing the recorded
    digest over the drifted bytes. Two assertions exclude it. The recorded
    digest is asserted *unchanged*, so an implementation that adopted the
    drifted one fails; and the bytes now on disk are asserted equal to the
    original, so an implementation that touched only the record fails. A green
    audit on its own is satisfied by both rivals and is therefore not the claim.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_drift.md"
    original = b"the bytes the caller delivered"
    on_disk = _write_source(minimal_config, sp, original)
    await gs.insert_document(_src_doc("deadbeef_drift", _sha256_of(original), source_path=sp))

    on_disk.write_bytes(b"something else wrote here")
    pre = await maint.verify_vault_source_files(check_hashes=True)
    assert pre.summary["hash_mismatch"] == 1, "the drift must be real before the repair"

    report = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", original))

    assert report.status == "restored"
    assert report.source_path == sp
    assert report.observed_content_hash == _sha256_of(b"something else wrote here")
    assert on_disk.read_bytes() == original

    doc = await gs.get_document("deadbeef_drift")
    assert doc.source_content_hash == _sha256_of(original)
    assert doc.stored_content_hash is None, (
        "the filesystem binding keeps what it was handed, so nothing licensed a "
        "digest refresh; a record rewritten here would be laundering, not repair"
    )

    post = await maint.verify_vault_source_files(check_hashes=True)
    assert post.summary["hash_mismatch"] == 0


async def test_restore_source_file_leaves_an_intact_copy_alone(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """Restoring over a copy that already matches its recorded digest writes
    nothing.

    Anti-coincidental-pass: an unconditional rewrite satisfies every
    audit-is-clean assertion, so the claim is pinned on the file's modification
    time and on the reported ``status``. Under a binding that rewrites at rest an
    unconditional write would also re-stamp the copy and churn the recorded
    digest for no repair, which is the cost this branch exists to avoid.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_intact.md"
    body = b"undisturbed content"
    on_disk = _write_source(minimal_config, sp, body)
    await gs.insert_document(_src_doc("deadbeef_intact", _sha256_of(body), source_path=sp))
    mtime_before = on_disk.stat().st_mtime_ns

    report = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", body))

    assert report.status == "already_intact"
    assert report.observed_content_hash == report.expected_content_hash
    assert on_disk.stat().st_mtime_ns == mtime_before, "an intact copy must not be rewritten"


async def test_restore_source_file_leaves_an_intact_diverged_copy_alone(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A retained copy that is intact but *not* byte-identical to what the caller
    delivered is still left alone.

    The shape a store that rewrites its copy at rest produces: provenance
    records the delivered bytes, the stored digest records the retained ones.

    Anti-coincidental-pass: the rival is an intact-check that compares the
    retained copy against ``source_content_hash`` instead of the stored digest.
    It is indistinguishable from the correct implementation wherever the two
    digests coincide -- which is every other test in this group, since the
    filesystem binding retains what it was handed -- and here it declares an
    intact copy drifted and rewrites it, re-stamping the copy and churning the
    recorded digest on every call. The two recorded digests are asserted to
    genuinely differ, so a fixture where they happened to agree cannot make this
    pass by accident.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_diverged.md"
    delivered = b"the bytes the caller handed over"
    retained = b"the bytes the store chose to keep"
    on_disk = _write_source(minimal_config, sp, retained)
    doc = _src_doc(
        "deadbeef_diverged",
        _sha256_of(delivered),
        source_path=sp,
        stored_content_hash=_sha256_of(retained),
    )
    assert doc.source_content_hash != doc.stored_content_hash
    await gs.insert_document(doc)
    mtime_before = on_disk.stat().st_mtime_ns

    report = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", delivered))

    assert report.status == "already_intact"
    assert on_disk.stat().st_mtime_ns == mtime_before
    assert on_disk.read_bytes() == retained


async def test_restore_source_file_lands_on_a_disambiguated_path(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A document a name collision moved aside is restored at *its* path, not at
    the path its basename would otherwise have been homed to.

    Covers the collision-disambiguated shape rather than a rival no other test
    excludes. The rival -- routing the delivered file through the binding's
    naming rule instead of reading the record's ``source_path`` -- is already
    caught by the drifted-copy test above, whose delivered basename differs from
    its recorded path. What is uncovered without this test is the *scenario*:
    the plain name is held by a **different, intact document**, so a restore
    that homed by basename would repair nothing and corrupt a bystander. That
    consequence, not the mechanism, is what the second assertion pins, and it is
    the reason this case is worth its own fixture: the same misroute that merely
    writes to a stray path elsewhere destroys real content here.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    squatter_bytes = b"a different document that got the plain name first"
    squatter = _write_source(minimal_config, "imports/deadbeef_twin.md", squatter_bytes)

    original = b"the bytes of the document that was moved aside"
    sp = "imports/deadbeef_twin_aabbccdd.md"
    on_disk = _write_source(minimal_config, sp, original)
    await gs.insert_document(_src_doc("deadbeef_moved", _sha256_of(original), source_path=sp))
    on_disk.write_bytes(b"drifted")

    report = await maint.restore_vault_source_file(
        _delivered(tmp_path, "deadbeef_twin.md", original)
    )

    assert report.source_path == sp
    assert on_disk.read_bytes() == original
    assert squatter.read_bytes() == squatter_bytes, (
        "restoring a disambiguated document must not touch the file at its planned path"
    )


async def test_restore_source_file_replaces_a_missing_copy(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A retained copy that vanished is put back, not merely reported."""
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_gone.md"
    body = b"deleted out of band"
    on_disk = _write_source(minimal_config, sp, body)
    await gs.insert_document(_src_doc("deadbeef_gone", _sha256_of(body), source_path=sp))
    on_disk.unlink()

    pre = await maint.verify_vault_source_files(check_hashes=True)
    assert pre.summary["missing"] == 1

    report = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", body))

    assert report.status == "restored"
    assert report.observed_content_hash is None
    assert on_disk.read_bytes() == body

    post = await maint.verify_vault_source_files(check_hashes=True)
    assert post.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


async def test_restore_source_file_refuses_bytes_no_document_claims(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """Bytes matching no document's provenance are refused, and nothing is
    written.

    Anti-coincidental-pass: the assertion is that the *existing* copy is
    untouched, not merely that an error was raised. An implementation that
    guessed a target and wrote to it would raise nothing and quietly corrupt an
    intact document -- the worst available failure -- and only a filesystem
    assertion catches it.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_bystander.md"
    body = b"an intact document"
    on_disk = _write_source(minimal_config, sp, body)
    await gs.insert_document(_src_doc("deadbeef_bystander", _sha256_of(body), source_path=sp))

    with pytest.raises(RestoreTargetUnresolvedError):
        await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", b"unknown bytes"))

    assert on_disk.read_bytes() == body
    post = await maint.verify_vault_source_files(check_hashes=True)
    assert post.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}


async def test_restore_source_file_pin_refuses_bytes_the_document_was_not_made_from(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A pin names which copy to write over; it does not license writing
    arbitrary bytes there.

    Without the provenance check on the pinned branch, this call overwrites the
    drifted copy with an unrelated file *and* the post-write refresh
    re-describes the record to match, so the integrity audit reports the
    document healthy while its stored bytes are something else entirely — the
    exact outcome every docstring on this path says repair must never produce,
    reached through a documented, encouraged input.

    Anti-coincidental-pass: the audit is run and asserted *red* afterwards, and
    the on-disk bytes are asserted to be the drifted ones. Raising alone is not
    the claim — an implementation that refused after writing, or that refused
    but left a laundered record behind, satisfies a `pytest.raises` check and
    fails these.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_pinned.md"
    original = b"the bytes this document was ingested from"
    on_disk = _write_source(minimal_config, sp, original)
    await gs.insert_document(
        _src_doc(
            "deadbeef_pinned",
            _sha256_of(original),
            source_path=sp,
            stored_content_hash=_sha256_of(original),
        )
    )
    drifted = b"something else wrote here"
    on_disk.write_bytes(drifted)

    with pytest.raises(RestoreProvenanceMismatchError):
        await maint.restore_vault_source_file(
            _delivered(tmp_path, "unrelated.md", b"COMPLETELY UNRELATED CONTENT"),
            document_id="deadbeef_pinned",
        )

    assert on_disk.read_bytes() == drifted, "the drifted copy must not be overwritten"
    doc = await gs.get_document("deadbeef_pinned")
    assert doc.stored_content_hash == _sha256_of(original), "the record must not move"
    report = await maint.verify_vault_source_files(check_hashes=True)
    assert report.summary["hash_mismatch"] == 1, (
        "the drift must stay reported; going green here is the laundering this guard exists to stop"
    )


async def test_restore_source_file_pre_split_pin_writes_but_does_not_launder_the_record(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """The pre-split exemption lets a pin through unverified, and the refresh
    rule is what stops that from laundering the record.

    A document ingested before delivered and stored digests were recorded
    separately carries a null ``stored_content_hash``, and its provenance hash
    describes the stored copy rather than the delivered bytes — so a caller
    re-delivering the original cannot match it, which is the case the pin exists
    to serve. The provenance check must therefore be skipped, leaving the write
    unguarded.

    Anti-coincidental-pass: this test isolates the *second* guard. The write is
    asserted to have happened (so the pre-split exemption is genuinely
    exercised, not short-circuited by the first guard), and the record is
    asserted unchanged with the audit still red — which only holds if the
    refresh is withheld when the store returns exactly what it was handed. An
    implementation carrying the first guard alone passes the mismatch test above
    and fails this one.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_presplit.md"
    recorded = b"what the record's provenance hash describes"
    on_disk = _write_source(minimal_config, sp, recorded)
    await gs.insert_document(_src_doc("deadbeef_presplit", _sha256_of(recorded), source_path=sp))
    assert (await gs.get_document("deadbeef_presplit")).stored_content_hash is None
    on_disk.write_bytes(b"drifted")

    wrong = b"not this document's bytes at all"
    report = await maint.restore_vault_source_file(
        _delivered(tmp_path, "wrong.md", wrong), document_id="deadbeef_presplit"
    )

    assert report.provenance_verified is False, (
        "nothing on a pre-split record can confirm the delivered file, and the "
        "report must say so rather than let the caller assume a check that did not run"
    )
    assert report.record_refreshed is False
    assert on_disk.read_bytes() == wrong, "the pre-split exemption must let the write through"
    doc = await gs.get_document("deadbeef_presplit")
    assert doc.stored_content_hash is None, (
        "a store that returned what it was handed licenses no digest refresh"
    )
    report = await maint.verify_vault_source_files(check_hashes=True)
    assert report.summary["hash_mismatch"] == 1, (
        "the mismatch must stay visible so the operator sees the repair did not take"
    )


async def test_restore_source_file_pin_accepts_correct_bytes_on_a_diverged_record(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A pin whose delivered bytes *are* the document's provenance is accepted,
    on a record whose stored digest differs from that provenance.

    Anti-coincidental-pass: the positive control the guard lacked. Every other
    succeeding pinned restore in this module has a null stored digest, so it
    takes the pre-split exemption and never reaches the comparison at all. Two
    rivals pass the whole suite without this case: a guard that refuses any pin
    once ``stored_content_hash`` is set, and one that compares the delivered
    digest against the *stored* digest instead of provenance — which under a
    store that rewrites at rest would refuse every legitimate pinned restore of
    the original bytes. The fixture separates the two digests precisely so the
    comparison has a wrong answer available to give.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_pinok.md"
    delivered = b"the bytes the caller originally handed over"
    retained = b"the bytes the store chose to keep"
    on_disk = _write_source(minimal_config, sp, retained)
    doc = _src_doc(
        "deadbeef_pinok",
        _sha256_of(delivered),
        source_path=sp,
        stored_content_hash=_sha256_of(retained),
    )
    assert doc.source_content_hash != doc.stored_content_hash
    await gs.insert_document(doc)
    on_disk.write_bytes(b"drifted")

    report = await maint.restore_vault_source_file(
        _delivered(tmp_path, "orig.md", delivered), document_id="deadbeef_pinok"
    )

    assert report.status == "restored"
    assert report.provenance_verified is True
    assert report.record_refreshed is False, (
        "the filesystem binding returns what it was handed, so nothing licensed "
        "a digest refresh even though a write happened"
    )
    assert on_disk.read_bytes() == delivered


async def test_restore_source_file_rejects_a_non_absolute_source(
    graph_store, minimal_config, stub_content_store
):
    """A relative source has no defined meaning on this operation and is refused
    rather than resolved against the server's working directory.

    On a deployed profile that directory is the container's, not the caller's,
    so a relative path silently reads a server-side file — and it slips past the
    caller-local delivery gate, which triggers on an absolute path.
    """
    maint = _maintenance_for(graph_store, minimal_config, content_store=stub_content_store)

    with pytest.raises(RestoreSourceNotAbsoluteError):
        await maint.restore_vault_source_file("relative/path.md")
    with pytest.raises(RestoreSourceNotAbsoluteError):
        await maint.restore_vault_source_file("~/originals/x.md")


async def test_restore_source_file_refusal_names_the_vault_relative_path(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A binding's refusal to write at the recorded path reaches the caller as
    the typed ``vault_source_path_refused`` error, and its message names the
    record's vault-relative path rather than the server's absolute one.

    Anti-coincidental-pass: the link's target holds *different* bytes and sits
    *inside* the source root, so the restore reaches the write (an intact copy
    is left alone) and containment accepts, leaving the symlink guard as the
    sole refuser. The message's positive half passes against the absolute
    spelling too, which ends in the same path; the negative half -- the storage
    root's absolute string is absent -- is the discriminator. Nothing may be
    written: the link and the copy it points at are asserted untouched.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    original = b"the bytes the caller delivered"
    sp = "imports/r.md"
    victim = _write_source(minimal_config, "imports/victim.md", b"VICTIM ORIGINAL")
    link = Path(minimal_config.vault.storage_root) / sp
    link.symlink_to(victim)
    await gs.insert_document(_src_doc("deadbeef_refused", _sha256_of(original), source_path=sp))

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await maint.restore_vault_source_file(_delivered(tmp_path, "r.md", original))

    assert sp in excinfo.value.message
    assert str(Path(minimal_config.vault.storage_root)) not in excinfo.value.message
    assert excinfo.value.detail == {"source_path": sp}
    assert link.is_symlink(), "a refusal must not disturb the tree"
    assert victim.read_bytes() == b"VICTIM ORIGINAL", "the link's target must be untouched"


async def test_restore_source_file_missing_delivered_source_raises_the_documented_error(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A ``source`` that names no readable file fails as the documented
    ``source_file_not_found``, not as a bare OSError.

    Anti-coincidental-pass: the error *type* is the claim. Reading the path
    without a guard also fails, but as a ``FileNotFoundError`` that the MCP
    boundary's ``except (SAGEError, ValueError)`` does not catch -- so the tool
    would raise through its envelope while both its docstring and the OpenAPI
    404 promise this code. Asserting ``pytest.raises(Exception)`` would pass
    under either, which is exactly why the assertion names the class.
    """
    maint = _maintenance_for(graph_store, minimal_config, content_store=stub_content_store)

    with pytest.raises(SourceFileNotFoundError):
        await maint.restore_vault_source_file(str(tmp_path / "never_written.md"))


async def test_restore_source_file_refuses_an_ambiguous_match_without_a_pin(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """Two documents sharing a provenance digest cannot be told apart from the
    bytes alone, so the restore refuses until named -- and the pin then restores
    exactly one of them.

    Anti-coincidental-pass: the second half is what gives the first half
    meaning. Refusing is only correct if the pin works; a service that refused
    unconditionally would pass a raises-only test. The untouched sibling is
    asserted too, so the pin is shown to select rather than to restore both.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    body = b"content two documents were made from"
    first = _write_source(minimal_config, "imports/deadbeef_twin_a.md", body)
    second = _write_source(minimal_config, "imports/deadbeef_twin_b.md", body)
    for doc_id, sp in (
        ("deadbeef_twin_a", "imports/deadbeef_twin_a.md"),
        ("deadbeef_twin_b", "imports/deadbeef_twin_b.md"),
    ):
        await gs.insert_document(_src_doc(doc_id, _sha256_of(body), source_path=sp))

    first.write_bytes(b"drifted")
    second.write_bytes(b"drifted")
    delivered = _delivered(tmp_path, "x.md", body)

    with pytest.raises(RestoreTargetUnresolvedError) as excinfo:
        await maint.restore_vault_source_file(delivered)
    assert sorted(excinfo.value.detail["candidate_ids"]) == [
        "deadbeef_twin_a",
        "deadbeef_twin_b",
    ]

    report = await maint.restore_vault_source_file(delivered, document_id="deadbeef_twin_a")

    assert report.document_id == "deadbeef_twin_a"
    assert first.read_bytes() == body
    assert second.read_bytes() == b"drifted", "the pin must select one, not restore both"


async def test_restore_source_file_lands_at_the_recorded_path(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """The bytes go to the path the record already names; no second copy appears
    alongside it.

    Anti-coincidental-pass: this is the whole reason the restore does not route
    through ``retain_source``. That method would see bytes differing from what
    sits at its target, read it as a name collision, and land the content at
    ``imports/<stem>_<hash8>.md`` -- leaving the drifted copy in place and the
    audit still red. Asserting the *absence of a sibling* is what distinguishes
    a write-in-place from a collision-handled retain; asserting the bytes alone
    would pass under both.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_place.md"
    body = b"original bytes"
    on_disk = _write_source(minimal_config, sp, body)
    await gs.insert_document(_src_doc("deadbeef_place", _sha256_of(body), source_path=sp))
    on_disk.write_bytes(b"drifted")

    report = await maint.restore_vault_source_file(_delivered(tmp_path, "place.md", body))

    imports = Path(minimal_config.vault.storage_root) / "imports"
    assert report.source_path == sp
    assert sorted(p.name for p in imports.glob("deadbeef_place*.md")) == ["deadbeef_place.md"]

    doc = await gs.get_document("deadbeef_place")
    assert doc.source_path == sp, "a restore must never re-home the document"


async def test_restore_source_file_refuses_a_symlinked_recorded_path(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """A recorded path that is a link is refused, however the bytes behind it
    hash.

    ``already_intact`` is a claim about the copy the record names, and a link is
    not that copy: the bytes live wherever its owner points, and the repair
    primitive will not write through it. Reporting the document fine would
    close the operator's only question over a record that cannot be repaired.

    Anti-coincidental-pass: the link's target holds exactly the recorded bytes,
    so the rival -- the short-circuit as it stood -- returns ``already_intact``
    for precisely this input. Asserting the raised type rather than merely "not
    already_intact" additionally excludes an implementation that fell through to
    some other non-intact report without refusing. The detail names the
    record's own path, so a refusal reported against the resolved target fails.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_rlink.md"
    body = b"the bytes the record describes"
    target = tmp_path / "elsewhere" / "real.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _link_source(minimal_config, sp, target)
    await gs.insert_document(_src_doc("deadbeef_rlink", _sha256_of(body), source_path=sp))

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await maint.restore_vault_source_file(_delivered(tmp_path, "r.md", body))

    assert excinfo.value.detail == {"source_path": sp}


async def test_restore_source_file_refusal_leaves_the_link_and_its_target_alone(
    graph_store, minimal_config, stub_content_store, tmp_path
):
    """The refusal happens before any write, so neither the link nor the file it
    points at is disturbed.

    Anti-coincidental-pass: the bytes behind the link hash to what the record
    expects, so this is the path the short-circuit used to take. A *drifted*
    target would be refused by the write guard that already existed, leaving
    this test green before the change and testing nothing. The rival is a
    refusal raised *after* the bytes landed, which following the link would
    rewrite whatever its owner points at; content cannot catch that here, since
    the delivered bytes are the target's own, but the modification time can.
    """
    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_untouched.md"
    recorded = b"what the record expects"
    target = tmp_path / "elsewhere" / "victim.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(recorded)
    link = _link_source(minimal_config, sp, target)
    await gs.insert_document(_src_doc("deadbeef_untouched", _sha256_of(recorded), source_path=sp))
    mtime_before = target.stat().st_mtime_ns

    with pytest.raises(VaultSourcePathRefusedError):
        await maint.restore_vault_source_file(_delivered(tmp_path, "u.md", recorded))

    assert link.is_symlink(), "a refusal must not disturb the tree"
    assert target.stat().st_mtime_ns == mtime_before, (
        "a rewrite of identical bytes is still a write through the link"
    )
    assert target.read_bytes() == recorded, "no write may land through the link"


async def test_audit_and_restore_agree_on_symlinked_through_one_observation(
    graph_store, minimal_config, stub_content_store, tmp_path, monkeypatch
):
    """Both surfaces take "is this path a link?" from the one shared
    observation, so they cannot disagree about it.

    The restore exists to repair what the audit reports. Were each to ask the
    store on its own, the two could drift apart -- the audit reporting a link
    the restore then writes over, or the reverse. The observation is replaced
    with one that *claims* a genuinely linked path is an ordinary intact copy;
    both surfaces must believe it.

    Anti-coincidental-pass: the path really is a link (the unpatched audit is
    asserted to report it), so either surface still asking the store itself sees
    the real link and breaks ranks -- the audit reporting `symlinked` against the
    faked observation, or the restore refusing. Patching over a clean fixture
    would leave both rivals green.
    """
    from sage.services.maintenance import _RetainedCopyObservation

    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_symagree.md"
    body = b"the bytes the record describes"
    target = tmp_path / "elsewhere" / "agree.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _link_source(minimal_config, sp, target)
    await gs.insert_document(_src_doc("deadbeef_symagree", _sha256_of(body), source_path=sp))

    pre = await maint.verify_vault_source_files(check_hashes=True)
    assert pre.summary["symlinked"] == 1, "the link must be real before the observation is faked"

    expected = _sha256_of(body)
    monkeypatch.setattr(
        MaintenanceService,
        "_observe_retained_copy",
        lambda self, doc, storage_root, store, *, hash_copy=True: _RetainedCopyObservation(
            present=True, observed_hash=expected, expected_hash=expected, symlinked=False
        ),
    )

    mtime_before = target.stat().st_mtime_ns
    audit = await maint.verify_vault_source_files(check_hashes=True)
    restore = await maint.restore_vault_source_file(_delivered(tmp_path, "a.md", body))

    assert audit.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}
    assert restore.status == "already_intact"
    # Content cannot carry the "not rewritten" claim here: the delivered bytes
    # are the target's own, so a restore that wrote through the link would leave
    # them equal. The modification time is what separates the two.
    assert target.stat().st_mtime_ns == mtime_before, (
        "a copy the observation calls intact is not rewritten"
    )
    assert target.read_bytes() == body


async def test_audit_and_restore_agree_on_intact_through_one_observation(
    graph_store, minimal_config, stub_content_store, tmp_path, monkeypatch
):
    """The audit and the restore take "intact" from one shared observation of
    the retained copy, so they cannot disagree about it.

    The restore exists to repair what the audit reports. Were each to look at
    the store on its own, the two could drift apart -- the audit sending an
    operator to repair a copy the restore then declares already intact, or the
    restore rewriting a copy the audit calls healthy. The observation is
    replaced with one that *claims* a genuinely drifted copy is intact; both
    surfaces must believe it.

    Anti-coincidental-pass: the copy on disk really is drifted (the unpatched
    audit is asserted to see it), so either function still doing its own
    ``source_exists`` / ``hash_source`` pair sees the real drift and breaks
    ranks -- the audit reporting a mismatch, or the restore writing. The file's
    bytes and modification time pin the restore side; a rewrite of the same
    drifted bytes would leave the content assertion green.
    """
    from sage.services.maintenance import _RetainedCopyObservation

    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_agree.md"
    original = b"the bytes the record describes"
    on_disk = _write_source(minimal_config, sp, original)
    await gs.insert_document(_src_doc("deadbeef_agree", _sha256_of(original), source_path=sp))
    on_disk.write_bytes(b"drifted")

    pre = await maint.verify_vault_source_files(check_hashes=True)
    assert pre.summary["hash_mismatch"] == 1, (
        "the drift must be real before the observation is faked"
    )

    expected = _sha256_of(original)
    monkeypatch.setattr(
        MaintenanceService,
        "_observe_retained_copy",
        lambda self, doc, storage_root, store, *, hash_copy=True: _RetainedCopyObservation(
            present=True, observed_hash=expected, expected_hash=expected
        ),
    )
    mtime_before = on_disk.stat().st_mtime_ns

    audit = await maint.verify_vault_source_files(check_hashes=True)
    restore = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", original))

    assert audit.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}
    assert restore.status == "already_intact"
    assert on_disk.read_bytes() == b"drifted", (
        "a copy the observation calls intact is not rewritten"
    )
    assert on_disk.stat().st_mtime_ns == mtime_before


async def test_audit_and_restore_agree_on_drift_through_one_observation(
    graph_store, minimal_config, stub_content_store, tmp_path, monkeypatch
):
    """The converse: an observation claiming drift over a genuinely intact copy
    is believed by both the audit and the restore, and the digest each reports
    is the observation's.

    Anti-coincidental-pass: the copy on disk really is intact (the unpatched
    audit is asserted clean), so a function doing its own hashing would report
    the copy healthy or leave it alone. The reported ``observed_content_hash``
    on both surfaces is asserted equal to the fabricated digest, which no
    second hashing of the real file could produce.
    """
    from sage.services.maintenance import _RetainedCopyObservation

    gs = graph_store
    maint = _maintenance_for(gs, minimal_config, content_store=stub_content_store)

    sp = "imports/deadbeef_agree.md"
    original = b"undisturbed bytes"
    on_disk = _write_source(minimal_config, sp, original)
    await gs.insert_document(_src_doc("deadbeef_agree", _sha256_of(original), source_path=sp))

    pre = await maint.verify_vault_source_files(check_hashes=True)
    assert pre.summary == {"healthy": 1, "missing": 0, "hash_mismatch": 0, "symlinked": 0}, (
        "the copy must be genuinely intact before the observation is faked"
    )

    expected = _sha256_of(original)
    fabricated = _sha256_of(b"not what is there")
    monkeypatch.setattr(
        MaintenanceService,
        "_observe_retained_copy",
        lambda self, doc, storage_root, store, *, hash_copy=True: _RetainedCopyObservation(
            present=True, observed_hash=fabricated, expected_hash=expected
        ),
    )

    audit = await maint.verify_vault_source_files(check_hashes=True)
    restore = await maint.restore_vault_source_file(_delivered(tmp_path, "x.md", original))

    assert audit.summary["hash_mismatch"] == 1
    assert audit.entries[0].observed_content_hash == fabricated
    assert restore.status == "restored"
    assert restore.observed_content_hash == fabricated
    assert on_disk.read_bytes() == original


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
