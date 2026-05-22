"""Graph Operations tests: BH-021, BH-023, BH-031 through BH-037.

Covers link (edge creation), check_preconditions (dependency validation),
traverse (graph walk with deduplication), and discover deterministic stub.
"""

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_core import PydanticUndefined

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeNotFoundError,
    PipelineIncompleteError,
    SelfReferentialEdgeError,
)
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    SourceType,
)
from sage.models.schemas import (
    ChainEntry,
    ChainRequest,
    Document,
    DocumentSummary,
    Edge,
    LinkRequest,
    TraversalNode,
    TraverseRequest,
)
from sage.storage.graph_store import GraphStore

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid LinkRequest / TraverseRequest / ChainRequest
    instances. Idempotent: an already-canonical id passes through
    unchanged so wrapping is safe to apply at every call site.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name.

    `EdgeIdStr` requires canonical-form UUIDs. Test fixtures historically
    used short readable strings ("edge_dep_001"); this helper maps any such
    name to a stable UUID so assertions can still anchor on names.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(
    doc_id: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    """Helper to create a Document with sensible defaults."""
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
    )


# ---------------------------------------------------------------------------
# BH-021: Failed document excluded from deterministic retrieval
# ---------------------------------------------------------------------------


async def test_bh_021_failed_doc_excluded_from_deterministic(graph_store, graph_ops_service):
    doc = _make_doc(_id("doc_failed"), pipeline_status=PipelineStatus.FAILED)
    doc.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc)

    with pytest.raises(PipelineIncompleteError) as exc_info:
        await graph_ops_service.check_pipeline_for_retrieval(_id("doc_failed"))
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "pipeline_incomplete"


# ---------------------------------------------------------------------------
# BH-023: Failed document does not satisfy preconditions
# ---------------------------------------------------------------------------


async def test_bh_023_failed_doc_does_not_satisfy_preconditions(graph_store, graph_ops_service):
    doc_function = _make_doc(_id("doc_function"))
    doc_dep = _make_doc(_id("doc_dep"), pipeline_status=PipelineStatus.FAILED)
    doc_dep.pipeline_error = "indexing failure"
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    # Create depends_on edge
    edge = Edge(
        id=_eid("edge_dep_001"),
        source_id=_id("doc_function"),
        target_id=_id("doc_dep"),
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions(_id("doc_function"))
    assert result.satisfied is False
    assert len(result.checks) == 1
    assert result.checks[0].target_id == _id("doc_dep")
    assert result.checks[0].satisfied is False
    assert "pipeline_incomplete" in result.checks[0].actual


# ---------------------------------------------------------------------------
# BH-031 (T-0079 update): Duplicate edges are now blocked
# ---------------------------------------------------------------------------
# Pre-T-0079, BH-031 asserted that re-calling `link` with the same
# natural-key triple produced a second edge with a fresh id. That is
# now an invariant violation; the UNIQUE constraint on
# (source_id, target_id, edge_type) prevents duplicate rows. The
# idempotent contract is exposed via `link_idempotent` (returns the
# pre-existing edge with `created=False`); non-idempotent `link`
# propagates `sqlite3.IntegrityError`.


async def test_bh_031_duplicate_edges_now_blocked(graph_store, graph_ops_service):
    import sqlite3 as _sqlite3

    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge1 = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="First rationale",
        )
    )
    with pytest.raises(_sqlite3.IntegrityError):
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_b"),
                rationale="Updated understanding",
            )
        )

    # Only the first edge survives.
    edges = await graph_store.get_edges_by_source(_id("doc_a"), "references")
    assert len(edges) == 1
    assert edges[0].id == edge1.id


# ---------------------------------------------------------------------------
# BH-032: Edge records have auto-generated IDs
# ---------------------------------------------------------------------------


async def test_bh_032_edge_auto_generated_id(graph_store, graph_ops_service):
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
        )
    )

    assert edge.id
    assert isinstance(edge.id, str)
    assert len(edge.id) > 0


# ---------------------------------------------------------------------------
# BH-033: check_preconditions -- active satisfies dependency
# ---------------------------------------------------------------------------


async def test_bh_033_active_satisfies_dependency(graph_store, graph_ops_service):
    doc_function = _make_doc(_id("doc_function"))
    doc_dep = _make_doc(_id("doc_dep"), lifecycle_status="active")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id=_eid("edge_dep_active"),
        source_id=_id("doc_function"),
        target_id=_id("doc_dep"),
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions(_id("doc_function"))
    assert result.satisfied is True
    assert result.checks[0].satisfied is True


# ---------------------------------------------------------------------------
# BH-034: check_preconditions -- completed satisfies dependency
# ---------------------------------------------------------------------------


async def test_bh_034_completed_satisfies_dependency(graph_store, graph_ops_service):
    doc_function = _make_doc(_id("doc_function"))
    doc_dep = _make_doc(_id("doc_dep"), lifecycle_status="completed")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id=_eid("edge_dep_completed"),
        source_id=_id("doc_function"),
        target_id=_id("doc_dep"),
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions(_id("doc_function"))
    assert result.satisfied is True


# ---------------------------------------------------------------------------
# BH-035: check_preconditions -- archived does not satisfy
# ---------------------------------------------------------------------------


async def test_bh_035_archived_does_not_satisfy(graph_store, graph_ops_service):
    doc_function = _make_doc(_id("doc_function"))
    doc_dep = _make_doc(_id("doc_dep"), lifecycle_status="archived")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id=_eid("edge_dep_archived"),
        source_id=_id("doc_function"),
        target_id=_id("doc_dep"),
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions(_id("doc_function"))
    assert result.satisfied is False
    assert result.checks[0].actual == "archived"


# ---------------------------------------------------------------------------
# BH-036: check_preconditions -- filed does not satisfy (domain-specific)
# ---------------------------------------------------------------------------


async def test_bh_036_filed_does_not_satisfy(graph_store, extended_graph_ops_service):
    doc_function = _make_doc(_id("doc_function"))
    doc_dep = _make_doc(_id("doc_dep"), lifecycle_status="filed")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id=_eid("edge_dep_filed"),
        source_id=_id("doc_function"),
        target_id=_id("doc_dep"),
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await extended_graph_ops_service.check_preconditions(_id("doc_function"))
    assert result.satisfied is False
    assert result.checks[0].actual == "filed"


# ---------------------------------------------------------------------------
# BH-037: Traversal collapses multi-path hits to one node per target
# ---------------------------------------------------------------------------
#
# Under T-0079, the UNIQUE (source_id, target_id, edge_type) constraint
# prevents storage-level duplicates, so the original BH-037 scenario
# (three duplicate edges between the same pair) is no longer
# constructable. The multi-path collapse responsibility of
# `_build_traversal_response` remains load-bearing: the SQL CTE can
# surface the same target document via different paths at different
# depths, and the dedup must still produce one TraversalNode per
# target with the minimum depth and a distinct-edge count per
# edge_type. A diamond topology exercises the surviving behavior.


async def test_bh_037_traversal_collapses_multipath_hits(graph_store, graph_ops_service):
    # Diamond: A -> B, A -> C, B -> D, C -> D.
    # Traversing from A with depth=2 reaches D via two paths (A->B->D
    # and A->C->D). Expected: one node for D with depth=2 and
    # edge_counts={"references": 2} (the two distinct B->D and C->D
    # edges, not the A->B and A->C ones, which appear under their own
    # target docs in result.nodes).
    for name in ("doc_a", "doc_b", "doc_c", "doc_d"):
        await graph_store.insert_document(_make_doc(_id(name)))

    base_time = datetime.now(timezone.utc) - timedelta(hours=4)
    edge_inputs = [
        ("edge_ab", "doc_a", "doc_b"),
        ("edge_ac", "doc_a", "doc_c"),
        ("edge_bd", "doc_b", "doc_d"),
        ("edge_cd", "doc_c", "doc_d"),
    ]
    for i, (label, src, tgt) in enumerate(edge_inputs):
        edge = Edge(
            id=_eid(label),
            source_id=_id(src),
            target_id=_id(tgt),
            edge_type=EdgeType.REFERENCES,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id(src),
            target_valid_from_version=_id(tgt),
            created_at=base_time + timedelta(hours=i),
            rationale=f"Rationale {label}",
        )
        await graph_store.insert_edge(edge)

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            edge_type=EdgeType.REFERENCES,
            depth=2,
        )
    )

    nodes_by_id = {node.document.id: node for node in result.nodes}
    assert _id("doc_b") in nodes_by_id
    assert _id("doc_c") in nodes_by_id
    assert _id("doc_d") in nodes_by_id

    # D is reached at depth=2 via two paths; collapse yields one node.
    d_node = nodes_by_id[_id("doc_d")]
    assert d_node.depth == 2
    assert d_node.edge_counts == {"references": 2}


# ---------------------------------------------------------------------------
# T-0079: insert_edge raises on duplicate natural-key triple
# ---------------------------------------------------------------------------


async def test_t0079_insert_edge_raises_on_duplicate(graph_store):
    import sqlite3 as _sqlite3

    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    first = Edge(
        id=_eid("edge_first"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("doc_a"),
        target_valid_from_version=_id("doc_b"),
        created_at=datetime.now(timezone.utc),
        rationale="First",
    )
    await graph_store.insert_edge(first)

    dup = Edge(
        id=_eid("edge_dup"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("doc_a"),
        target_valid_from_version=_id("doc_b"),
        created_at=datetime.now(timezone.utc),
        rationale="Second",
    )
    with pytest.raises(_sqlite3.IntegrityError):
        await graph_store.insert_edge(dup, on_conflict="raise")


async def test_t0079_insert_edge_noop_returns_existing(graph_store):
    """on_conflict="noop": duplicate insert returns existing edge."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    first = Edge(
        id=_eid("edge_first"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("doc_a"),
        target_valid_from_version=_id("doc_b"),
        created_at=datetime.now(timezone.utc),
        rationale="First",
    )
    stored_first, created_first = await graph_store.insert_edge(first)
    assert created_first is True
    assert stored_first.id == first.id

    dup = Edge(
        id=_eid("edge_dup"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("doc_a"),
        target_valid_from_version=_id("doc_b"),
        created_at=datetime.now(timezone.utc),
        rationale="Second",
    )
    stored_dup, created_dup = await graph_store.insert_edge(dup, on_conflict="noop")
    # No-op: the dup payload was discarded; the original is returned.
    assert created_dup is False
    assert stored_dup.id == first.id
    assert stored_dup.rationale == "First"


async def test_t0079_link_idempotent_returns_existing(graph_store, graph_ops_service):
    """graph_ops.link_idempotent: second call returns the existing edge."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    edge_1, created_1 = await graph_ops_service.link_idempotent(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="initial rationale",
        )
    )
    assert created_1 is True
    assert edge_1.rationale == "initial rationale"

    edge_2, created_2 = await graph_ops_service.link_idempotent(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="DIFFERENT rationale on second call",
        )
    )
    assert created_2 is False
    # The pre-existing edge is returned; rationale is preserved.
    assert edge_2.id == edge_1.id
    assert edge_2.rationale == "initial rationale"


async def test_t0079_link_still_raises_on_duplicate(graph_store, graph_ops_service):
    """graph_ops.link (non-idempotent) propagates the IntegrityError."""
    import sqlite3 as _sqlite3

    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="first",
        )
    )
    with pytest.raises(_sqlite3.IntegrityError):
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_b"),
                rationale="second",
            )
        )


async def test_t0079_multiple_retracts_with_null_target_allowed(graph_store, graph_ops_service):
    """SQLite UNIQUE treats NULL as distinct: multiple retracts edges
    on the same source with target_id=NULL stay legal per ADR-017.
    The retracted_edge_id differs across retract instances.
    """
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))
    await graph_store.insert_document(_make_doc(_id("doc_c")))

    # Two distinct covers edges that can each be retracted.
    covers_b = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
        )
    )
    covers_c = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_c"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_c"),
        )
    )

    # Retract both: both retracts edges share source_id=doc_a,
    # target_id=NULL, edge_type='retracts'. Under SQLite NULL-distinct
    # semantics this is legal.
    retract_b = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_a"),
            retracted_edge_id=covers_b.id,
        )
    )
    retract_c = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_a"),
            retracted_edge_id=covers_c.id,
        )
    )
    assert retract_b.id != retract_c.id


async def test_bh_037_legacy_three_duplicate_edges_storage_blocked(graph_store):
    """T-0079 contract: the legacy BH-037 setup (three duplicate edges
    between the same pair via direct INSERT) is now blocked at the
    storage layer. This test pins the post-T-0079 invariant.
    """
    import sqlite3 as _sqlite3

    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    base_time = datetime.now(timezone.utc) - timedelta(hours=3)
    first = Edge(
        id=_eid("edge_first"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("doc_a"),
        target_valid_from_version=_id("doc_b"),
        created_at=base_time,
        rationale="first",
    )
    await graph_store.insert_edge(first)

    for i in range(1, 3):
        dup = Edge(
            id=_eid(f"edge_dup_{i}"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=base_time + timedelta(hours=i),
            rationale=f"rationale {i}",
        )
        with pytest.raises(_sqlite3.IntegrityError):
            await graph_store.insert_edge(dup)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


async def test_link_self_referential_raises_400(graph_store, graph_ops_service):
    doc_a = _make_doc(_id("doc_a"))
    await graph_store.insert_document(doc_a)

    with pytest.raises(SelfReferentialEdgeError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_a"),
                edge_type=EdgeType.REFERENCES,
            )
        )
    assert exc_info.value.status_code == 400


async def test_link_nonexistent_source_raises_404(graph_store, graph_ops_service):
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_b)

    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("nonexistent"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.REFERENCES,
            )
        )


async def test_link_nonexistent_target_raises_404(graph_store, graph_ops_service):
    doc_a = _make_doc(_id("doc_a"))
    await graph_store.insert_document(doc_a)

    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("nonexistent"),
                edge_type=EdgeType.REFERENCES,
            )
        )


async def test_traverse_nonexistent_start_raises_404(graph_store, graph_ops_service):
    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.traverse(TraverseRequest(start_id=_id("nonexistent")))


async def test_check_preconditions_nonexistent_function_raises_404(graph_store, graph_ops_service):
    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.check_preconditions("nonexistent")


# ---------------------------------------------------------------------------
# BH-089: Linear supersedes chain returns ordered version history
# ---------------------------------------------------------------------------


async def _create_linear_chain(graph_store, count: int = 5):
    """Create a linear supersedes chain of `count` documents.

    Returns list of doc IDs in order [v1, v2, ..., vN] where each
    supersedes its predecessor (source=newer, target=older).
    """
    doc_ids = []
    for i in range(1, count + 1):
        doc = _make_doc(_id(f"v{i}"))
        doc.version_label = f"v{i}"
        doc.document_date = f"2026-01-{i:02d}"
        await graph_store.insert_document(doc)
        doc_ids.append(_id(f"v{i}"))

    # Each version supersedes its predecessor: v2->v1, v3->v2, etc.
    for i in range(1, count):
        edge = Edge(
            id=_eid(f"edge_sup_{i}"),
            source_id=_id(f"v{i + 1}"),
            target_id=_id(f"v{i}"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=datetime.now(timezone.utc),
        )
        await graph_store.insert_edge(edge)
    return doc_ids


async def test_bh_089_linear_chain_ordered(graph_store, graph_ops_service):
    await _create_linear_chain(graph_store, 5)

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("v3"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 5
    assert result.is_linear is True
    assert result.head_id == _id("v5")
    assert result.tail_id == _id("v1")
    assert result.query_position == 2
    # Verify ordering: positions 0..4 map to v1..v5
    for i, entry in enumerate(result.chain):
        assert entry.position == i
        assert entry.id == _id(f"v{i + 1}")
        assert entry.version_label == f"v{i + 1}"


# ---------------------------------------------------------------------------
# BH-090: Chain walk from head document
# ---------------------------------------------------------------------------


async def test_bh_090_chain_from_head(graph_store, graph_ops_service):
    await _create_linear_chain(graph_store, 5)

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("v5"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 5
    assert result.query_position == 4
    assert result.head_id == _id("v5")
    assert result.tail_id == _id("v1")


# ---------------------------------------------------------------------------
# BH-091: Chain walk from tail document
# ---------------------------------------------------------------------------


async def test_bh_091_chain_from_tail(graph_store, graph_ops_service):
    await _create_linear_chain(graph_store, 5)

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("v1"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 5
    assert result.query_position == 0
    assert result.head_id == _id("v5")
    assert result.tail_id == _id("v1")


# ---------------------------------------------------------------------------
# BH-092: Single-node chain (no edges of requested type)
# ---------------------------------------------------------------------------


async def test_bh_092_single_node_chain(graph_store, graph_ops_service):
    doc = _make_doc(_id("doc_solo"))
    await graph_store.insert_document(doc)

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_solo"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 1
    assert result.is_linear is True
    assert result.head_id == _id("doc_solo")
    assert result.tail_id == _id("doc_solo")
    assert result.query_position == 0
    assert result.chain[0].id == _id("doc_solo")


# ---------------------------------------------------------------------------
# BH-093: Fork detection sets is_linear false
# ---------------------------------------------------------------------------


async def test_bh_093_fork_detection(graph_store, graph_ops_service):
    # doc_a is the common predecessor; doc_b and doc_c both supersede doc_a
    for name in ["doc_a", "doc_b", "doc_c"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    for i, src in enumerate(["doc_b", "doc_c"]):
        edge = Edge(
            id=_eid(f"edge_fork_{i}"),
            source_id=_id(src),
            target_id=_id("doc_a"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=datetime.now(timezone.utc),
        )
        await graph_store.insert_edge(edge)

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_a"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.is_linear is False
    assert result.length == 3
    chain_ids = {e.id for e in result.chain}
    assert chain_ids == {_id("doc_a"), _id("doc_b"), _id("doc_c")}


# ---------------------------------------------------------------------------
# BH-094: Chain ignores other edge types
# ---------------------------------------------------------------------------


async def test_bh_094_chain_ignores_other_edge_types(graph_store, graph_ops_service):
    for name in ["doc_a", "doc_b", "doc_c"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    # doc_a supersedes doc_b
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=datetime.now(timezone.utc),
        )
    )
    # doc_a covers doc_c (different edge type)
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_cov"),
            source_id=_id("doc_a"),
            target_id=_id("doc_c"),
            edge_type=EdgeType.COVERS,
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_a"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 2
    chain_ids = {e.id for e in result.chain}
    assert chain_ids == {_id("doc_a"), _id("doc_b")}
    assert _id("doc_c") not in chain_ids


# ---------------------------------------------------------------------------
# BH-095: Chain with non-existent document returns 404
# ---------------------------------------------------------------------------


async def test_bh_095_chain_nonexistent_document(graph_store, graph_ops_service):
    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.chain(
            ChainRequest(
                document_id=_id("nonexistent"),
                edge_type=EdgeType.SUPERSEDES,
            )
        )


# ---------------------------------------------------------------------------
# BH-096: Chain works with non-supersedes edge types
# ---------------------------------------------------------------------------


async def test_bh_096_chain_with_references(graph_store, graph_ops_service):
    for name in ["doc_a", "doc_b", "doc_c"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    # doc_a -> doc_b -> doc_c via references
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_ref_1"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_ref_2"),
            source_id=_id("doc_b"),
            target_id=_id("doc_c"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
        )
    )

    assert result.length == 3
    assert result.is_linear is True
    assert result.query_position == 1


# ---------------------------------------------------------------------------
# BH-097: edge_counts map with mixed edge types
# ---------------------------------------------------------------------------


async def test_bh_097_edge_counts_mixed_types(graph_store, graph_ops_service):
    # T-0079 reshape: a single (source, target, edge_type) row per
    # natural-key triple is enforced. The "mixed types" invariant is
    # preserved by attaching one of each edge_type between doc_a and
    # doc_b; the edge_counts map should record both keys.
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    now = datetime.now(timezone.utc)
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=now,
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_cov"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.COVERS,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=now,
        )
    )

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            direction="outbound",
            depth=1,
        )
    )

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.edge_counts == {"supersedes": 1, "covers": 1}


# ---------------------------------------------------------------------------
# BH-098: Single edge type produces single-key map
# ---------------------------------------------------------------------------


async def test_bh_098_edge_counts_single_type(graph_store, graph_ops_service):
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_ref"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            direction="outbound",
            depth=1,
        )
    )

    assert len(result.nodes) == 1
    assert result.nodes[0].edge_counts == {"references": 1}


# ---------------------------------------------------------------------------
# BH-099: Filtered traversal shows only filtered type in counts
# ---------------------------------------------------------------------------


async def test_bh_099_edge_counts_filtered(graph_store, graph_ops_service):
    # T-0079 reshape: one edge per natural-key triple. The invariant
    # under test (edge_type filter excludes other types from counts)
    # holds with single-row attachments.
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    now = datetime.now(timezone.utc)
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=now,
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_cov"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.COVERS,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=now,
        )
    )

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            edge_type=EdgeType.SUPERSEDES,
            direction="outbound",
            depth=1,
        )
    )

    assert len(result.nodes) == 1
    assert result.nodes[0].edge_counts == {"supersedes": 1}
    assert "covers" not in result.nodes[0].edge_counts


# ---------------------------------------------------------------------------
# BH-100: Multi-depth traversal with per-node edge_counts
# ---------------------------------------------------------------------------


async def test_bh_100_edge_counts_multi_depth(graph_store, graph_ops_service):
    # T-0079 reshape: at most one (source, target, edge_type) per
    # natural-key triple. Multi-depth invariant is preserved with
    # one of each edge_type per hop.
    for name in ["doc_a", "doc_b", "doc_c"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    now = datetime.now(timezone.utc)
    # doc_a -> doc_b: one supersedes + one covers
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup_ab"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=now,
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_cov_ab"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.COVERS,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=now,
        )
    )

    # doc_b -> doc_c: one references edge.
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_ref_bc"),
            source_id=_id("doc_b"),
            target_id=_id("doc_c"),
            edge_type=EdgeType.REFERENCES,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_b"),
            target_valid_from_version=_id("doc_c"),
            created_at=now,
        )
    )

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            direction="outbound",
            depth=2,
        )
    )

    nodes_by_id = {n.document.id: n for n in result.nodes}
    assert nodes_by_id[_id("doc_b")].edge_counts == {"supersedes": 1, "covers": 1}
    assert nodes_by_id[_id("doc_c")].edge_counts == {"references": 1}


# ---------------------------------------------------------------------------
# Unlink (production edge deletion)
# ---------------------------------------------------------------------------


async def test_unlink_deletes_existing_edge(graph_store, graph_ops_service):
    """unlink removes a production edge and returns confirmation."""
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
        )
    )

    result = await graph_ops_service.unlink(edge.id)
    assert result.deleted is True
    assert result.edge_id == edge.id

    # Edge no longer exists in the store
    edges = await graph_store.get_edges_by_source(_id("doc_a"))
    assert len(edges) == 0


async def test_unlink_nonexistent_edge_raises_404(graph_store, graph_ops_service):
    """unlink raises EdgeNotFoundError for a nonexistent edge_id."""
    with pytest.raises(EdgeNotFoundError) as exc_info:
        await graph_ops_service.unlink("nonexistent_edge")
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "edge_not_found"


# ---------------------------------------------------------------------------
# GraphStore get_edge / delete_edge
# ---------------------------------------------------------------------------


async def test_get_edge_returns_edge(graph_store):
    """get_edge returns the Edge when it exists."""
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge = Edge(
        id=_eid("edge_get_test"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_store.get_edge(_eid("edge_get_test"))
    assert result is not None
    assert result.id == _eid("edge_get_test")
    assert result.source_id == _id("doc_a")
    assert result.target_id == _id("doc_b")


async def test_get_edge_returns_none_for_missing(graph_store):
    """get_edge returns None for a nonexistent edge_id."""
    result = await graph_store.get_edge("nonexistent")
    assert result is None


async def test_delete_edge_returns_true(graph_store):
    """delete_edge returns True and removes the row."""
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge = Edge(
        id=_eid("edge_del_test"),
        source_id=_id("doc_a"),
        target_id=_id("doc_b"),
        edge_type=EdgeType.REFERENCES,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    assert await graph_store.delete_edge(_eid("edge_del_test")) is True
    assert await graph_store.get_edge(_eid("edge_del_test")) is None


async def test_delete_edge_returns_false_for_missing(graph_store):
    """delete_edge returns False for a nonexistent edge_id."""
    assert await graph_store.delete_edge("nonexistent") is False


# ---------------------------------------------------------------------------
# Chain with no matching edges includes available_edge_types hint
# ---------------------------------------------------------------------------


async def test_chain_single_node_shows_available_edge_types(graph_store, graph_ops_service):
    """Chain of length 1 includes available_edge_types when other edges exist."""
    for name in ["doc_x", "doc_y"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    # doc_x has a references edge but no supersedes edges
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_ref_only"),
            source_id=_id("doc_x"),
            target_id=_id("doc_y"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_x"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 1
    assert result.available_edge_types is not None
    assert "references" in result.available_edge_types


async def test_chain_single_node_no_edges_at_all(graph_store, graph_ops_service):
    """Chain of length 1 with no edges of any type has empty available_edge_types."""
    await graph_store.insert_document(_make_doc(_id("doc_isolated")))

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_isolated"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 1
    assert result.available_edge_types is not None
    assert result.available_edge_types == []


async def test_chain_with_matching_edges_no_hint(graph_store, graph_ops_service):
    """Chain with actual matching edges has available_edge_types as None."""
    for name in ["doc_p", "doc_q"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup_pq"),
            source_id=_id("doc_p"),
            target_id=_id("doc_q"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("doc_p"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 2
    assert result.available_edge_types is None


# ---------------------------------------------------------------------------
# Chain slice parameters: limit and offset
# ---------------------------------------------------------------------------


async def test_chain_slice_limit(graph_store, graph_ops_service):
    """Chain with limit returns only the requested number of entries."""
    # Create a 4-version chain: v1 <- v2 <- v3 <- v4 (newest)
    for i in range(1, 5):
        await graph_store.insert_document(_make_doc(_id(f"sv{i}")))
    for i in range(1, 4):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_sv_{i}"),
                source_id=_id(f"sv{i + 1}"),
                target_id=_id(f"sv{i}"),
                edge_type=EdgeType.SUPERSEDES,
                created_at=datetime.now(timezone.utc),
            )
        )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("sv1"),
            edge_type=EdgeType.SUPERSEDES,
            limit=2,
        )
    )

    # Full chain is 4, but we requested 2
    assert len(result.chain) == 2
    assert result.total_length == 4
    # Default offset=0 means oldest first: sv1, sv2
    assert result.chain[0].id == _id("sv1")
    assert result.chain[1].id == _id("sv2")


async def test_chain_slice_offset_and_limit(graph_store, graph_ops_service):
    """Chain with offset+limit returns the correct slice."""
    for i in range(1, 5):
        await graph_store.insert_document(_make_doc(_id(f"so{i}")))
    for i in range(1, 4):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_so_{i}"),
                source_id=_id(f"so{i + 1}"),
                target_id=_id(f"so{i}"),
                edge_type=EdgeType.SUPERSEDES,
                created_at=datetime.now(timezone.utc),
            )
        )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("so1"),
            edge_type=EdgeType.SUPERSEDES,
            limit=2,
            offset=1,
        )
    )

    assert len(result.chain) == 2
    assert result.total_length == 4
    # offset=1: skip sv1, get sv2, sv3
    assert result.chain[0].id == _id("so2")
    assert result.chain[1].id == _id("so3")


async def test_chain_no_slice_returns_full(graph_store, graph_ops_service):
    """Chain without limit/offset returns all entries and total_length == length."""
    for i in range(1, 4):
        await graph_store.insert_document(_make_doc(_id(f"sf{i}")))
    for i in range(1, 3):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_sf_{i}"),
                source_id=_id(f"sf{i + 1}"),
                target_id=_id(f"sf{i}"),
                edge_type=EdgeType.SUPERSEDES,
                created_at=datetime.now(timezone.utc),
            )
        )

    result = await graph_ops_service.chain(
        ChainRequest(
            document_id=_id("sf1"),
            edge_type=EdgeType.SUPERSEDES,
        )
    )

    assert result.length == 3
    assert result.total_length == 3
    assert len(result.chain) == 3


# ---------------------------------------------------------------------------
# T-0124: Parity test for the BH-101-excluded ``Edge`` CTE-row construction.
#
# The traversal hot path at sage/services/graph_ops.py:663 constructs an
# ``Edge`` directly from a CTE join row, deliberately bypassing the
# canonical factory ``GraphStore._row_to_edge`` per the BH-101 performance
# rationale (per-row ``model_validate`` cost on a thousands-of-rows hot
# path). Per the *CAS Projection-Point Audit Conventions* steering
# document (cas vault, doc_type=steering_document), excluded projection
# points still owe a structural guard against field-addition drift
# between the two paths: when ``Edge`` grows a field and one path is
# updated but the other is not, the guard must trip.
#
# The exhaustive-fields test on the canonical factory itself lives in
# tests/sage/test_graph_store.py::test_row_to_edge_populates_every_edge_field
# (T-0123). This file installs the parity half of the same closure: a
# test that iterates ``Edge.model_fields`` and asserts field-by-field
# equality between an ``Edge`` built via ``_row_to_edge`` and an ``Edge``
# built via the graph_ops.py:663 inline construction from equivalent
# row inputs.
# ---------------------------------------------------------------------------


import sqlite3  # noqa: E402 -- co-located with the T-0124 fixtures below

# Shared sentinel values used to populate both the canonical
# ``sqlite3.Row`` (matching the ``_row_to_edge`` shape) and the CTE row
# dict (matching the graph_ops.py:663 shape). Per the cohort decision
# sheet, sentinel fixtures are per-ticket local -- no shared module --
# but within a single ticket the same sentinel constants drive both
# halves of the parity assertion so the equality check is meaningful.
_T0124_EDGE_ID = str(uuid.UUID(int=0xED9E0000_0000_0000_0000_00000000C124))
_T0124_RETRACTED_EDGE_ID = str(uuid.UUID(int=0xED9E0000_0000_0000_0000_00000000C125))
_T0124_SOURCE_ID = _id("t0124_doc_source")
_T0124_TARGET_ID = _id("t0124_doc_target")
_T0124_SOURCE_ANCHOR = _id("t0124_doc_source_anchor")
_T0124_TARGET_ANCHOR = _id("t0124_doc_target_anchor")
_T0124_TOMBSTONE = _id("t0124_doc_tombstone")
_T0124_CREATED_AT_ISO = datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat()
_T0124_EDGE_TYPE = EdgeType.REFERENCES.value
_T0124_RESOLUTION_POLICY = ResolutionPolicy.TRANSITIVE_BOTH.value
_T0124_RATIONALE_KIND = RationaleKind.VERSION_CHAIN.value
_T0124_NOTES = "t0124 sentinel notes"
_T0124_RATIONALE = "t0124 sentinel rationale"
_T0124_SYNCED_FROM_VERSION = _id("t0124_doc_synced_from_version")
_T0124_SYNCED_FROM_CONTENT_HASH = "sha256:" + "cd" * 32


def _edge_row_with_every_edge_field() -> sqlite3.Row:
    """Per-ticket sentinel ``sqlite3.Row`` matching the ``_row_to_edge``
    column shape, with every field set to a distinct non-default value.

    Co-derived with ``_edge_cte_row_with_every_edge_field`` from the same
    underlying sentinel constants so the two row shapes carry equivalent
    payloads despite their different column-name conventions
    (``id`` / ``created_at`` here vs. ``edge_id`` / ``edge_created_at``
    in the CTE row dict).
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
                ? AS id,
                ? AS source_id,
                ? AS target_id,
                ? AS edge_type,
                ? AS resolution_policy,
                ? AS source_valid_from_version,
                ? AS target_valid_from_version,
                ? AS valid_until_version,
                ? AS retracted_edge_id,
                ? AS created_at,
                ? AS notes,
                ? AS rationale,
                ? AS rationale_kind,
                ? AS synced_from_version,
                ? AS synced_from_content_hash
            """,
            (
                _T0124_EDGE_ID,
                _T0124_SOURCE_ID,
                _T0124_TARGET_ID,
                _T0124_EDGE_TYPE,
                _T0124_RESOLUTION_POLICY,
                _T0124_SOURCE_ANCHOR,
                _T0124_TARGET_ANCHOR,
                _T0124_TOMBSTONE,
                _T0124_RETRACTED_EDGE_ID,
                _T0124_CREATED_AT_ISO,
                _T0124_NOTES,
                _T0124_RATIONALE,
                _T0124_RATIONALE_KIND,
                _T0124_SYNCED_FROM_VERSION,
                _T0124_SYNCED_FROM_CONTENT_HASH,
            ),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _edge_cte_row_with_every_edge_field() -> dict:
    """Per-ticket sentinel dict matching the CTE row shape consumed at
    sage/services/graph_ops.py:663, with every field set to a distinct
    non-default value drawn from the same constants as
    ``_edge_row_with_every_edge_field``.

    The column-name conventions differ from the ``_row_to_edge`` shape:
    the traversal CTE aliases the edge-id and created-at columns as
    ``edge_id`` and ``edge_created_at`` to avoid collision with the
    document join's own ``id``/``created_at`` columns. The remaining
    storage-layer fields keep their bare names.
    """
    return {
        "edge_id": _T0124_EDGE_ID,
        "source_id": _T0124_SOURCE_ID,
        "target_id": _T0124_TARGET_ID,
        "edge_type": _T0124_EDGE_TYPE,
        "resolution_policy": _T0124_RESOLUTION_POLICY,
        "source_valid_from_version": _T0124_SOURCE_ANCHOR,
        "target_valid_from_version": _T0124_TARGET_ANCHOR,
        "valid_until_version": _T0124_TOMBSTONE,
        "retracted_edge_id": _T0124_RETRACTED_EDGE_ID,
        "edge_created_at": _T0124_CREATED_AT_ISO,
        "notes": _T0124_NOTES,
        "rationale": _T0124_RATIONALE,
        "rationale_kind": _T0124_RATIONALE_KIND,
        "synced_from_version": _T0124_SYNCED_FROM_VERSION,
        "synced_from_content_hash": _T0124_SYNCED_FROM_CONTENT_HASH,
    }


def _build_edge_from_cte_row(representative: dict) -> Edge:
    """Emulate the inline ``Edge`` construction at
    sage/services/graph_ops.py:663 verbatim.

    Mirrors the production code field-by-field so the parity assertion
    detects drift between the two construction paths when ``Edge`` grows
    a field. If the production inline construction changes its kwargs,
    this emulation must change in lockstep -- the parity test will
    surface the divergence as a field-name-specific assertion failure.
    """
    resolution_policy_raw = representative.get("resolution_policy")
    return Edge(
        id=representative["edge_id"],
        source_id=representative["source_id"],
        target_id=representative["target_id"],
        edge_type=EdgeType(representative["edge_type"]),
        resolution_policy=(
            ResolutionPolicy(resolution_policy_raw) if resolution_policy_raw is not None else None
        ),
        source_valid_from_version=representative.get("source_valid_from_version"),
        target_valid_from_version=representative.get("target_valid_from_version"),
        valid_until_version=representative.get("valid_until_version"),
        retracted_edge_id=representative.get("retracted_edge_id"),
        created_at=datetime.fromisoformat(representative["edge_created_at"]),
        notes=representative["notes"],
        rationale=representative["rationale"],
        rationale_kind=RationaleKind(representative["rationale_kind"]),
        synced_from_version=representative.get("synced_from_version"),
        synced_from_content_hash=representative.get("synced_from_content_hash"),
    )


def test_edge_cte_row_parity_with_row_to_edge():
    """T-0124 (F4 closure pair, T2 -- parity guard on the BH-101 excluded
    projection point): the inline ``Edge`` construction at
    sage/services/graph_ops.py:663 and the canonical factory
    ``GraphStore._row_to_edge`` (sage/storage/graph_store.py) construct
    field-equivalent ``Edge`` instances from equivalent row inputs.

    The exhaustive-fields test on the canonical factory itself is T-0123
    (``tests/sage/test_graph_store.py::``
    ``test_row_to_edge_populates_every_edge_field``). This parity test
    composes against the same sentinel-row pattern and adds the
    drift-detection half: iterating ``Edge.model_fields`` and asserting
    equality at every field guarantees that when a future field is
    added to ``Edge`` and wired through one path but not the other, the
    parity check trips.

    Together the T-0123 exhaustive-fields test and this T-0124 parity
    test satisfy the three sub-criteria for an excluded projection
    point under the *CAS Projection-Point Audit Conventions* steering
    document (the third sub-criterion -- the in-line BH-101 exclusion
    comment at the construction site -- lives in
    sage/services/graph_ops.py adjacent to the inline ``Edge`` block).
    """
    row = _edge_row_with_every_edge_field()
    cte_row = _edge_cte_row_with_every_edge_field()

    edge_canonical = GraphStore._row_to_edge(row)
    edge_inline = _build_edge_from_cte_row(cte_row)

    # Both halves of the parity must produce identical Edge instances.
    # Iterating ``Edge.model_fields`` is the structural closure: a field
    # added to Edge automatically becomes part of the assertion.
    divergences = []
    for field_name in Edge.model_fields:
        canonical_value = getattr(edge_canonical, field_name)
        inline_value = getattr(edge_inline, field_name)
        if canonical_value != inline_value:
            divergences.append(
                f"Edge.{field_name}: canonical={canonical_value!r} inline={inline_value!r}"
            )
    assert not divergences, (
        "Edge inline construction at sage/services/graph_ops.py:663 "
        "diverged from canonical GraphStore._row_to_edge factory on "
        f"the following fields: {divergences}. The two paths must "
        "remain field-equivalent per the BH-101 exclusion guard "
        "(T-0124, T-0123) in the *CAS Projection-Point Audit "
        "Conventions* steering document."
    )

    # Belt-and-suspenders: also assert structural equality at the model
    # level. This catches anything ``model_fields`` enumeration might
    # miss (extra/dropped fields would already trip pydantic validation
    # before reaching this line; equality is the residual check).
    assert edge_canonical == edge_inline


# ---------------------------------------------------------------------------
# T-0120: ChainEntry.from_chain_row factory closes the chain-walk CTE-row
# projection. The exhaustive-fields test (T1) is the structural F4 closure:
# it fails closed when a new field is added to ChainEntry without a matching
# factory update.
# ---------------------------------------------------------------------------


def _chain_row_with_every_chain_entry_field() -> dict:
    """Build a chain-walk row dict with every ChainEntry-mapped key set to a
    distinct non-default sentinel. Used by the exhaustive-fields test to
    verify ``ChainEntry.from_chain_row`` populates each field; a row whose
    nullable keys (``version_label``, ``document_date``) are left at their
    default ``None`` would let the test pass coincidentally on a buggy
    factory that drops one of them."""
    return {
        "doc_id": _id("chain_sentinel"),
        "title": "Sentinel chain title",
        "version_label": "v1.7",
        "lifecycle_status": "archived",
        "document_date": "2026-05-19",
    }


# T1: exhaustive fields — the keystone F4-closure test.
def test_from_chain_row_populates_every_chain_entry_field():
    row = _chain_row_with_every_chain_entry_field()
    entry = ChainEntry.from_chain_row(row, position=3)
    # Three-branch closure-test idiom (T-0144). ChainEntry has no
    # list/dict or non-None-default scalar fields today; both branches
    # are forward defense for future field additions.
    for field_name, field_info in ChainEntry.model_fields.items():
        value = getattr(entry, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"ChainEntry.{field_name} not populated by from_chain_row "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"ChainEntry.{field_name} matches its default ({default!r}) — "
                "from_chain_row may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, f"ChainEntry.{field_name} not populated by from_chain_row"


# ---------------------------------------------------------------------------
# T-0119: TraversalNode.from_traversal closure-pair install.
# Single owning factory consolidates the TraversalNode construction site at
# sage/services/graph_ops.py:693. The exhaustive-fields test below is the
# structural F4 closure: it iterates TraversalNode.model_fields and fails
# closed if a future field is added to the model but not wired through the
# factory. Per the *CAS Projection-Point Audit Conventions* steering
# document (cas vault, doc_type=steering_document).
# ---------------------------------------------------------------------------


def _document_summary_with_every_field() -> DocumentSummary:
    """Build a DocumentSummary with every field set to a distinct non-default
    sentinel. Local to this ticket's closure test; the cohort policy is
    per-ticket sentinels with no shared module."""
    return DocumentSummary(
        id=_id("traversal_doc"),
        title="Traversal Doc Title",
        lifecycle_status="active",
        source_type=SourceType.MARKDOWN,
        source_path="imports/traversal-doc.md",
        version_label="v1.0",
        project="proj-T",
        doc_type="ticket",
        tags=["alpha", "beta"],
        document_date=datetime(2026, 5, 15, tzinfo=timezone.utc),
        source_modified_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        semantic_abstract="an abstract for the traversal sentinel",
        tier3_metadata={"ticket_id": "T-0119", "ticket_priority": "high"},
    )


def _edge_with_every_field() -> Edge:
    """Build an Edge with every field set to a distinct non-default sentinel.
    Local to this ticket's closure test."""
    return Edge(
        id=str(uuid.uuid4()),
        source_id=_id("traversal_src"),
        target_id=_id("traversal_doc"),
        edge_type=EdgeType.REFERENCES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("traversal_src_v1"),
        target_valid_from_version=_id("traversal_doc_v1"),
        valid_until_version=_id("traversal_doc_v2"),
        retracted_edge_id=str(uuid.uuid4()),
        created_at=datetime(2026, 5, 21, 9, 30, tzinfo=timezone.utc),
        notes="sentinel notes",
        rationale="sentinel rationale",
        rationale_kind=RationaleKind.VERSION_CHAIN,
    )


def test_from_traversal_populates_every_traversal_node_field():
    """Exhaustive-fields closure: every TraversalNode field must be
    populated by ``TraversalNode.from_traversal`` when given non-default
    sentinel inputs. List-typed and dict-typed fields must be truthy
    (anti-coincidental: an empty default would pass a naive
    ``is not None`` check). When a future field is added to TraversalNode
    without a matching factory update, this test fails closed."""
    document = _document_summary_with_every_field()
    edge = _edge_with_every_field()
    depth = 3
    edge_counts = {"references": 2, "supersedes": 1}

    node = TraversalNode.from_traversal(
        document=document,
        edge=edge,
        depth=depth,
        edge_counts=edge_counts,
    )

    # Three-branch closure-test idiom (T-0144). The list/dict branch is
    # extended to ``dict[str, int]`` for ``TraversalNode.edge_counts``
    # (default_factory={} — not addressed by the non-None-default-scalar
    # branch). The elif branch is forward defense; TraversalNode has no
    # non-None-default scalar fields today.
    for field_name, field_info in TraversalNode.model_fields.items():
        value = getattr(node, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == dict[str, int] or annotation == list[str]:
            assert value, (
                f"TraversalNode.{field_name} not populated by from_traversal "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"TraversalNode.{field_name} matches its default ({default!r}) — "
                "from_traversal may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, f"TraversalNode.{field_name} not populated by from_traversal"


# ---------------------------------------------------------------------------
# T-0118: DocumentSummary.from_traversal_row closure-pair install.
# Single owning factory consolidates the DocumentSummary construction site at
# sage/services/graph_ops.py:628, which built DocumentSummary directly from a
# CTE join row's ``d_*``-prefixed columns bypassing the canonical
# ``DocumentSummary.from_document`` factory (T-0096). Per the *Projection-Point
# Closure Cohort — Canonical Decisions (T-0109 Follow-up Set)* reference
# document, T-0118 routes via a fresh ``from_traversal_row`` classmethod on
# DocumentSummary (no per-row Document construction on the traversal hot path,
# yielding two parallel factories each with its own exhaustive-fields test).
# The exhaustive-fields test below is the structural F4 closure: it iterates
# DocumentSummary.model_fields and fails closed if a future field is added to
# the model but not wired through the new factory.
# ---------------------------------------------------------------------------


def _traversal_row_with_every_document_summary_field() -> dict:
    """Per-ticket sentinel CTE row dict with every ``d_*``-prefixed key set to
    a distinct non-default value. Local to T-0118; the cohort policy is
    per-ticket sentinels with no shared module.

    The sentinel includes ``d_semantic_abstract`` and ``d_tier3_metadata``
    keys even though the production SQL (sage/storage/graph_store.py) does
    not currently surface those columns. That divergence is by design and
    out of scope per the T-0118 ticket body ("Refactoring the broader
    CTE-join column-naming convention" is out of scope). The factory reads
    these keys defensively via ``dict.get`` so production traversal rows
    that omit them continue to project ``None`` for the corresponding
    DocumentSummary fields; the sentinel populates the keys non-null so
    the closure test detects factory-side drift on field additions
    independently of when the SQL is extended to carry the new columns.
    """
    return {
        "doc_id": _id("t0118_doc_traversal_sentinel"),
        "d_title": "T-0118 Traversal Sentinel Title",
        "d_lifecycle_status": "active",
        "d_source_type": SourceType.MARKDOWN.value,
        "d_source_path": "imports/t0118-traversal-sentinel.md",
        "d_version_label": "v1.0",
        "d_project": "proj-t0118",
        "d_doc_type": "ticket",
        "d_tags": '["alpha", "beta"]',  # JSON-encoded as the production CTE row carries it
        "d_document_date": "2026-05-15",
        "d_source_modified_at": datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
        "d_semantic_abstract": "T-0118 sentinel semantic abstract",
        "d_tier3_metadata": {"ticket_id": "T-0118", "ticket_priority": "high"},
    }


def test_from_traversal_row_populates_every_document_summary_field():
    """Exhaustive-fields closure: every DocumentSummary field must be
    populated by ``DocumentSummary.from_traversal_row`` when given a
    sentinel CTE row dict whose ``d_*`` keys are all set to distinct
    non-default values. List-typed and dict-typed fields must be truthy
    (anti-coincidental: an empty default would pass a naive
    ``is not None`` check). When a future field is added to
    DocumentSummary without a matching factory update, this test fails
    closed (T-0118)."""
    row = _traversal_row_with_every_document_summary_field()
    summary = DocumentSummary.from_traversal_row(row)
    # Three-branch closure-test idiom (T-0144). DocumentSummary has no
    # non-None-default scalar fields today; the elif is forward defense.
    for field_name, field_info in DocumentSummary.model_fields.items():
        value = getattr(summary, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"DocumentSummary.{field_name} not populated by from_traversal_row "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"DocumentSummary.{field_name} matches its default ({default!r}) — "
                "from_traversal_row may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, (
                f"DocumentSummary.{field_name} not populated by from_traversal_row"
            )
