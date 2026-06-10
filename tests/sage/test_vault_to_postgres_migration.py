"""Tests for the LanceDB/SQLite -> Postgres vault migration tool (CAS-ADR-042).

Two layers:

- Pure reconciliation tests (no database) exercise the comparison functions and
  the report's pass/fail conjunction directly on hand-built records.
- Integration tests build a real SQLite + LanceDB source vault in ``tmp_path``,
  migrate it into the scratch Postgres named by ``SAGE_TEST_PG_DSN``, and assert
  graph identity, chain-head reconstruction, vector fidelity, idempotency, the
  tier3 index activation, and source immutability. They skip cleanly when no
  test Postgres is configured.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk, EdgeReadFailure, GraphStore
from sage.config import VaultConfig
from sage.migration.vault_to_postgres import (
    AbstractReconciliation,
    ChainHeadReconciliation,
    ChunkReconciliation,
    DocumentReconciliation,
    EdgeReconciliation,
    IdSetReconciliation,
    VaultMigrationReport,
    migrate_vault,
    reconcile_abstracts,
    reconcile_chain_heads,
    reconcile_chunks,
    reconcile_documents,
    reconcile_edges,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType, UserType
from sage.models.schemas import Document, Edge, StagingEdge, User
from sage.storage.graph_store import SqliteGraphStore
from sage.storage.postgres.graph_store import PostgresGraphStore

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
_DIM = 768

_USER_ID = "55555555-5555-4555-8555-555555555555"
_E_SUPERSEDES = "11111111-1111-4111-8111-111111111111"
_E_DERIVED = "22222222-2222-4222-8222-222222222222"
_E_RETRACTS = "33333333-3333-4333-8333-333333333333"
_E_STAGING = "44444444-4444-4444-8444-444444444444"

# A deliberately non-UUID edge id, the shape a hand-repaired row that predates
# boundary validation carries on disk.
_BAD_EDGE_ID = "deadbeef_manual_repair"
_EMPTY_TIER3_DOC = "eeeeeeee_empty"


def _hash(suffix: str) -> str:
    """A canonical sha256-shaped hash derived from a one-char suffix."""
    return "sha256:" + (suffix * 64)[:64]


def _doc(doc_id: str, content_hash: str, **over: object) -> Document:
    fields: dict[str, object] = {
        "id": doc_id,
        "title": f"Doc {doc_id}",
        "source_type": SourceType.MARKDOWN,
        "source_path": f"imports/{doc_id}.md",
        "lifecycle_status": "active",
        "source_content_hash": content_hash,
        "adapter_version": "0.5.0",
        "created_by": _USER_ID,
        "created_at": _NOW,
        "last_modified_by": _USER_ID,
        "updated_at": _NOW,
        "projected_at": _NOW,
        "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE,
    }
    fields.update(over)
    return Document(**fields)  # type: ignore[arg-type]


def _edge(edge_id: str, source_id: str, target_id: str | None, **over: object) -> Edge:
    fields: dict[str, object] = {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": EdgeType.SUPERSEDES,
        "created_at": _NOW,
    }
    fields.update(over)
    return Edge(**fields)  # type: ignore[arg-type]


def _embedding(seed: int) -> list[float]:
    """A deterministic, distinctive, non-zero 768-dim vector for a chunk."""
    return [float((seed * 31 + j) % 97) / 97.0 for j in range(_DIM)]


# ---------------------------------------------------------------------------
# A. Pure reconciliation (no database)
# ---------------------------------------------------------------------------


def test_reconcile_documents_identical_sets_ok():
    src = [_doc("aaaaaaaa_a", _hash("a")), _doc("bbbbbbbb_b", _hash("b"))]
    tgt = [_doc("aaaaaaaa_a", _hash("a")), _doc("bbbbbbbb_b", _hash("b"))]
    result = reconcile_documents(src, tgt)
    assert result.ok is True
    assert result.source_count == result.target_count == 2
    assert result.missing == [] and result.extra == [] and result.field_mismatch == []
    assert result.hash_mismatch == []


def test_reconcile_documents_missing_doc_flags_not_ok():
    src = [_doc("aaaaaaaa_a", _hash("a")), _doc("bbbbbbbb_b", _hash("b"))]
    tgt = [_doc("aaaaaaaa_a", _hash("a"))]
    result = reconcile_documents(src, tgt)
    assert result.ok is False
    assert result.missing == ["bbbbbbbb_b"]
    assert result.source_count == 2 and result.target_count == 1


def test_reconcile_documents_hash_mismatch_flagged():
    src = [_doc("aaaaaaaa_a", _hash("a"))]
    tgt = [_doc("aaaaaaaa_a", _hash("b"))]  # same id, different content hash
    result = reconcile_documents(src, tgt)
    assert result.ok is False
    assert [m.document_id for m in result.hash_mismatch] == ["aaaaaaaa_a"]
    assert result.hash_mismatch[0].source_hash == _hash("a")
    assert result.hash_mismatch[0].target_hash == _hash("b")


def test_reconcile_edges_identity_equality_ok():
    src = [_edge(_E_SUPERSEDES, "bbbbbbbb_b", "aaaaaaaa_a")]
    tgt = [_edge(_E_SUPERSEDES, "bbbbbbbb_b", "aaaaaaaa_a")]
    assert reconcile_edges(src, tgt).ok is True


def test_reconcile_edges_detects_missing_and_anchor_drift():
    src = [
        _edge(_E_SUPERSEDES, "bbbbbbbb_b", "aaaaaaaa_a"),
        _edge(
            _E_DERIVED,
            "cccccccc_c",
            "bbbbbbbb_b",
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version="cccccccc_c",
        ),
    ]
    # Target is missing the supersedes edge and has a drifted anchor on the other.
    tgt = [
        _edge(
            _E_DERIVED,
            "cccccccc_c",
            "bbbbbbbb_b",
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version="dddddddd_d",  # anchor drift
        ),
    ]
    result = reconcile_edges(src, tgt)
    assert result.ok is False
    assert result.missing == [_E_SUPERSEDES]
    drift = [m for m in result.field_mismatch if m.edge_id == _E_DERIVED]
    assert any(m.field == "source_valid_from_version" for m in drift)


def test_reconcile_chain_heads_map_equality():
    src = {"aaaaaaaa_a": False, "bbbbbbbb_b": True}
    assert reconcile_chain_heads(src, dict(src)).ok is True
    diverged = reconcile_chain_heads(src, {"aaaaaaaa_a": True, "bbbbbbbb_b": True})
    assert diverged.ok is False
    assert diverged.divergent == ["aaaaaaaa_a"]


def test_reconcile_chunks_per_doc_count():
    ok = reconcile_chunks({"aaaaaaaa_a": 3, "bbbbbbbb_b": 2}, {"aaaaaaaa_a": 3, "bbbbbbbb_b": 2})
    assert ok.ok is True and ok.source_total == ok.target_total == 5
    short = reconcile_chunks({"aaaaaaaa_a": 3}, {"aaaaaaaa_a": 1})
    assert short.ok is False
    assert short.per_doc_mismatch[0].document_id == "aaaaaaaa_a"
    assert short.per_doc_mismatch[0].source_count == 3
    assert short.per_doc_mismatch[0].target_count == 1


def test_reconcile_chunks_vector_mismatch_flags_not_ok():
    result = reconcile_chunks({"aaaaaaaa_a": 2}, {"aaaaaaaa_a": 2}, vector_mismatch=["aaaaaaaa_a"])
    assert result.ok is False
    assert result.vector_mismatch == ["aaaaaaaa_a"]


def test_reconcile_abstract_coverage():
    src = [_doc("aaaaaaaa_a", _hash("a"), semantic_abstract="x"), _doc("bbbbbbbb_b", _hash("b"))]
    same = reconcile_abstracts(src, list(src))
    assert same.ok is True and same.source_coverage == same.target_coverage == 1
    tgt = [_doc("aaaaaaaa_a", _hash("a")), _doc("bbbbbbbb_b", _hash("b"))]  # abstract dropped
    dropped = reconcile_abstracts(src, tgt)
    assert dropped.ok is False
    assert dropped.source_coverage == 1 and dropped.target_coverage == 0


def _all_ok_report(executed: bool = True) -> VaultMigrationReport:
    return VaultMigrationReport(
        vault_id="v",
        executed=executed,
        documents=DocumentReconciliation(source_count=1, target_count=1),
        edges=EdgeReconciliation(source_count=1, target_count=1),
        chain_heads=ChainHeadReconciliation(source_count=1, target_count=1),
        chunks=ChunkReconciliation(source_total=1, target_total=1),
        abstracts=AbstractReconciliation(total_documents=1, source_coverage=1, target_coverage=1),
        users=IdSetReconciliation(source_count=1, target_count=1),
        staging_edges=IdSetReconciliation(source_count=0, target_count=0),
    )


def test_report_ok_is_conjunction():
    assert _all_ok_report().ok is True
    # Flip one sub-reconciliation: the whole report must fail.
    broken = _all_ok_report()
    broken.documents = DocumentReconciliation(source_count=2, target_count=1, missing=["x"])
    assert broken.ok is False
    # A blocked tier3 index also fails the report.
    blocked = _all_ok_report()
    blocked.tier3_indexes_blocked = [
        {"doc_type": "ticket", "field": "ticket_id", "message": "collision"}  # type: ignore[list-item]
    ]
    assert blocked.ok is False
    # A dry-run (not executed) is never a pass verdict.
    assert _all_ok_report(executed=False).ok is False


def test_reconcile_documents_treats_empty_tier3_as_null():
    # The Postgres adapter normalizes an empty tier3 dict to NULL on insert, so
    # a faithful copy of a `{}` source row reads back as None. Same semantics,
    # two representations: the comparison must not flag it.
    src = [_doc("aaaaaaaa_a", _hash("a"), tier3_metadata={})]
    tgt = [_doc("aaaaaaaa_a", _hash("a"), tier3_metadata=None)]
    result = reconcile_documents(src, tgt)
    assert result.ok is True
    assert result.field_mismatch == []
    assert result.tier3_empty_normalized == ["aaaaaaaa_a"]


def test_reconcile_documents_real_tier3_mismatch_still_flagged():
    # Normalization covers exactly `{}` vs None; a populated dict against None
    # is a genuine fidelity failure and must keep flagging.
    src = [_doc("aaaaaaaa_a", _hash("a"), tier3_metadata={"k": "v"})]
    tgt = [_doc("aaaaaaaa_a", _hash("a"), tier3_metadata=None)]
    result = reconcile_documents(src, tgt)
    assert result.ok is False
    assert result.field_mismatch == ["aaaaaaaa_a"]
    assert result.tier3_empty_normalized == []


def test_report_ok_fails_on_invalid_source_edges():
    report = _all_ok_report()
    assert report.ok is True
    report.invalid_source_edges = [
        EdgeReadFailure(
            raw_id=_BAD_EDGE_ID,
            source_id="dddddddd_dep",
            target_id="cccccccc_ref",
            edge_type="references",
            error="edge_id is not a well-formed edge id",
        )
    ]
    assert report.ok is False


async def test_lenient_edge_read_default_passthrough():
    # The ABC default delegates to the strict read and reports no failures, so
    # stores whose rows were validated at insert time need no override.
    class _StrictOnly:
        async def get_edges_by_source(self, source_id, edge_type=None):
            return [_edge(_E_SUPERSEDES, "bbbbbbbb_b", "aaaaaaaa_a")]

    edges, failures = await GraphStore.get_edges_by_source_with_failures(
        _StrictOnly(), "bbbbbbbb_b"
    )
    assert [e.id for e in edges] == [_E_SUPERSEDES]
    assert failures == []


# ---------------------------------------------------------------------------
# B. Integration (real Postgres; skips without SAGE_TEST_PG_DSN)
# ---------------------------------------------------------------------------


def _ticket_config() -> VaultConfig:
    """A vault config with a ``ticket`` doc_type carrying a unique ``ticket_id``."""
    return VaultConfig.model_validate(
        {
            "vault": {
                "id": "testvault",
                "name": "Test Vault",
                "owner": "tester",
                "storage_root": "/tmp/testvault/sources",
                "brain_root": "/tmp/testvault/brain",
                "visibility": "personal",
            },
            "document_types": {
                "doc_types": [
                    {
                        "value": "ticket",
                        "label": "Ticket",
                        "metadata_schema": {
                            "type": "object",
                            "properties": {"ticket_id": {"type": "string"}},
                        },
                        "unique_keys": ["ticket_id"],
                    },
                    {"value": "note", "label": "Note"},
                    {"value": "memo", "label": "Memo"},
                ],
            },
            "lifecycle": {
                "base_states_required": True,
                "states": [
                    {"value": "active", "label": "Active"},
                    {"value": "archived", "label": "Archived", "is_terminal": True},
                ],
                "transitions": [
                    {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                    {
                        "from_state": "active",
                        "action": "supersede",
                        "to_state": "archived",
                        "creates_edge": "supersedes",
                    },
                ],
            },
            "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
            "metadata_extraction": {},
            "edge_inference": {},
        }
    )


def _inject_malformed_edge(db_path: Path) -> None:
    """Write an edge row with a non-UUID id straight into the store file.

    Bypasses the model layer on purpose: such rows exist only where history
    predates boundary validation, and the migration must report them rather
    than crash, so the fixture has to stage them below the validated API.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, edge_type, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_BAD_EDGE_ID, "dddddddd_dep", "cccccccc_ref", "references", _NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _force_empty_tier3(db_path: Path, doc_id: str) -> None:
    """Set a document's stored tier3_metadata to the literal ``'{}'``.

    ``insert_document`` normalizes falsy tier3 to NULL, but the metadata-update
    path serializes whatever dict it is given, so real vaults carry ``'{}'``
    rows; stage that on-disk state directly.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE documents SET tier3_metadata = '{}' WHERE id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


async def _build_source_vault(
    tmp_path, *, malformed_edge: bool = False, empty_tier3_doc: bool = False
):
    """Seed a real SQLite + LanceDB source vault and return (graph, content)."""
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    graph = SqliteGraphStore(brain / "graph.db")
    await graph.initialize()
    content = LanceDBContentStore(brain)

    await graph.insert_user(
        User(id=_USER_ID, display_name="Tester", user_type=UserType.HUMAN, created_at=_NOW)
    )

    pred = _doc(
        "aaaaaaaa_pred",
        _hash("a"),
        doc_type="ticket",
        lifecycle_status="archived",
        tier3_metadata={"ticket_id": "T-9001"},
        semantic_abstract="superseded predecessor abstract",
    )
    head = _doc(
        "bbbbbbbb_head",
        _hash("b"),
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-9001"},
        semantic_abstract="active head abstract",
    )
    ref = _doc("cccccccc_ref", _hash("c"), doc_type="note", tags=["alpha", "beta"])
    dep = _doc("dddddddd_dep", _hash("d"), doc_type="memo")
    for doc in (pred, head, ref, dep):
        await graph.insert_document(doc)

    # head supersedes pred -> pred.is_chain_head flips to false (SQLite trigger).
    await graph.insert_edge(_edge(_E_SUPERSEDES, "bbbbbbbb_head", "aaaaaaaa_pred"))
    await graph.insert_edge(
        _edge(
            _E_DERIVED,
            "cccccccc_ref",
            "bbbbbbbb_head",
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version="cccccccc_ref",
        )
    )
    # A retracts edge carries a null target_id (exercises the nullable-target path).
    await graph.insert_edge(
        _edge(
            _E_RETRACTS,
            "dddddddd_dep",
            None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id=_E_DERIVED,
        )
    )

    await graph.insert_staging_edge(
        StagingEdge(
            id=_E_STAGING,
            source_id="cccccccc_ref",
            target_id="dddddddd_dep",
            edge_type=EdgeType.REFERENCES,
            inference_evidence="filename token match",
            created_at=_NOW,
        )
    )

    await content.index_chunks(
        "bbbbbbbb_head",
        [
            Chunk("bbbbbbbb_head", "Intro", "head intro", _embedding(1), 0, "ticket", "active"),
            Chunk("bbbbbbbb_head", "Body", "head body", _embedding(2), 1, "ticket", "active"),
        ],
    )
    await content.index_chunks(
        "cccccccc_ref",
        [Chunk("cccccccc_ref", "Note", "a note", _embedding(3), 0, "note", "active")],
    )

    if empty_tier3_doc:
        await graph.insert_document(_doc(_EMPTY_TIER3_DOC, _hash("e"), doc_type="note"))
        _force_empty_tier3(brain / "graph.db", _EMPTY_TIER3_DOC)
    if malformed_edge:
        _inject_malformed_edge(brain / "graph.db")
    return graph, content


async def _target_count(pool, table: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        row = await cur.fetchone()
    return int(row[0])


async def _run_migration(
    tmp_path, pg_pool, *, execute: bool, malformed_edge: bool = False, empty_tier3_doc: bool = False
) -> tuple[VaultMigrationReport, object, object]:
    source_graph, source_content = await _build_source_vault(
        tmp_path, malformed_edge=malformed_edge, empty_tier3_doc=empty_tier3_doc
    )
    target_graph = PostgresGraphStore(pg_pool)
    target_content = PostgresContentStore(pg_pool)
    report = await migrate_vault(
        source_graph=source_graph,
        source_content=source_content,
        target_graph=target_graph,
        target_content=target_content,
        target_pool=pg_pool,
        config=_ticket_config(),
        vault_id="testvault",
        execute=execute,
    )
    return report, source_graph, source_content


async def test_migrate_vault_round_trips_graph(tmp_path, pg_pool):
    report, source_graph, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    assert report.ok is True, report.model_dump()

    target_graph = PostgresGraphStore(pg_pool)
    src_ids = {d.id for d in await source_graph.list_all_documents()}
    tgt_ids = {d.id for d in await target_graph.list_all_documents()}
    assert (
        src_ids
        == tgt_ids
        == {
            "aaaaaaaa_pred",
            "bbbbbbbb_head",
            "cccccccc_ref",
            "dddddddd_dep",
        }
    )
    assert report.documents.field_mismatch == []
    assert report.edges.ok and report.edges.source_count == report.edges.target_count == 3
    assert report.users.ok and report.staging_edges.ok


async def test_migrate_vault_reconstructs_chain_heads(tmp_path, pg_pool):
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    assert report.chain_heads.ok is True
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT id, is_chain_head FROM documents ORDER BY id")
        rows = dict(await cur.fetchall())
    assert rows["aaaaaaaa_pred"] is False  # superseded predecessor
    assert rows["bbbbbbbb_head"] is True  # chain head


async def test_migrate_vault_copies_vectors_and_chunks(tmp_path, pg_pool):
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    assert report.chunks.ok is True
    assert report.chunks.source_total == report.chunks.target_total == 3
    assert report.chunks.vector_mismatch == []
    # Vectors landed (the head's two chunks plus the note's one).
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
        row = await cur.fetchone()
    assert int(row[0]) == 3


async def test_migrate_vault_is_idempotent(tmp_path, pg_pool):
    report1, _, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    assert report1.ok is True
    # Second run against the now-populated schema must not collide and stay ok.
    source_graph, source_content = await _build_source_vault(tmp_path / "again")
    report2 = await migrate_vault(
        source_graph=source_graph,
        source_content=source_content,
        target_graph=PostgresGraphStore(pg_pool),
        target_content=PostgresContentStore(pg_pool),
        target_pool=pg_pool,
        config=_ticket_config(),
        vault_id="testvault",
        execute=True,
    )
    assert report2.ok is True
    assert report2.documents.target_count == 4
    assert await _target_count(pg_pool, "documents") == 4


async def test_migrate_vault_creates_tier3_unique_index(tmp_path, pg_pool):
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    assert "ticket.ticket_id" in report.tier3_indexes_created
    assert report.tier3_indexes_blocked == []
    target_graph = PostgresGraphStore(pg_pool)
    assert await target_graph.tier3_unique_index_exists("ticket", "ticket_id") is True


async def test_migrate_vault_leaves_source_intact(tmp_path, pg_pool):
    source_graph, source_content = await _build_source_vault(tmp_path)
    before_docs = await source_graph.get_total_document_count()
    before_edges = await source_graph.get_total_edge_count()
    before_chunks = await source_content.count_chunks()

    report = await migrate_vault(
        source_graph=source_graph,
        source_content=source_content,
        target_graph=PostgresGraphStore(pg_pool),
        target_content=PostgresContentStore(pg_pool),
        target_pool=pg_pool,
        config=_ticket_config(),
        vault_id="testvault",
        execute=True,
    )
    assert report.ok is True
    assert report.documents.target_count == before_docs  # target grew to match source
    assert await source_graph.get_total_document_count() == before_docs
    assert await source_graph.get_total_edge_count() == before_edges
    assert await source_content.count_chunks() == before_chunks


async def test_migrate_vault_dry_run_writes_nothing(tmp_path, pg_pool):
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=False)
    assert report.executed is False
    assert report.documents.source_count == 4
    assert report.documents.target_count == 0
    assert await _target_count(pg_pool, "documents") == 0
    assert await _target_count(pg_pool, "chunks") == 0


async def test_migrated_hashes_resolve_via_port(tmp_path, pg_pool):
    _, source_graph, _ = await _run_migration(tmp_path, pg_pool, execute=True)
    target_graph = PostgresGraphStore(pg_pool)
    source_hashes = [d.source_content_hash for d in await source_graph.list_all_documents()]
    resolved = await target_graph.find_documents_by_hashes(source_hashes)
    assert set(resolved.keys()) == set(source_hashes)


async def test_migrate_vault_contains_malformed_edge(tmp_path, pg_pool):
    """One schema-violating source edge fails the verdict, not the run."""
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=True, malformed_edge=True)
    assert report.ok is False
    assert [f.raw_id for f in report.invalid_source_edges] == [_BAD_EDGE_ID]
    failure = report.invalid_source_edges[0]
    assert failure.source_id == "dddddddd_dep"
    assert failure.target_id == "cccccccc_ref"
    assert failure.edge_type == "references"
    assert failure.error
    # The valid edge set still round-trips faithfully around the bad row.
    assert report.edges.ok is True
    assert report.edges.source_count == report.edges.target_count == 3
    assert await _target_count(pg_pool, "edges") == 3


async def test_migrate_vault_empty_dict_tier3_reconciles_clean(tmp_path, pg_pool):
    """A source doc whose stored tier3 is ``'{}'`` reconciles against NULL."""
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=True, empty_tier3_doc=True)
    assert report.ok is True, report.model_dump()
    assert report.documents.ok is True
    assert report.documents.tier3_empty_normalized == [_EMPTY_TIER3_DOC]


async def test_dry_run_reports_invalid_edges(tmp_path, pg_pool):
    """Dry-run enumerates invalid source edges, making it the data-quality probe."""
    report, _, _ = await _run_migration(tmp_path, pg_pool, execute=False, malformed_edge=True)
    assert report.executed is False
    assert [f.raw_id for f in report.invalid_source_edges] == [_BAD_EDGE_ID]
    assert await _target_count(pg_pool, "edges") == 0
