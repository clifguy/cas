"""Graph Operations tests: BH-021, BH-023, BH-031 through BH-037.

Covers link (edge creation), check_preconditions (dependency validation),
traverse (graph walk with deduplication), and discover deterministic stub.
"""

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeNotFoundError,
    PipelineIncompleteError,
    SelfReferentialEdgeError,
)
from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import ChainRequest, Document, Edge, LinkRequest, TraverseRequest

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
# BH-031: Duplicate edges are permitted
# ---------------------------------------------------------------------------


async def test_bh_031_duplicate_edges_permitted(graph_store, graph_ops_service):
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
    edge2 = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="Updated understanding",
        )
    )

    assert edge1.id != edge2.id

    # Both edges exist in the store
    edges = await graph_store.get_edges_by_source(_id("doc_a"), "references")
    assert len(edges) == 2


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
# BH-037: Traversal deduplicates by document with edge_counts map
# ---------------------------------------------------------------------------


async def test_bh_037_traversal_deduplicates_with_edge_counts(graph_store, graph_ops_service):
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    base_time = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(3):
        edge = Edge(
            id=_eid(f"edge_ref_{i}"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            created_at=base_time + timedelta(hours=i),
            rationale=f"Rationale {i}",
        )
        await graph_store.insert_edge(edge)

    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("doc_a"),
            edge_type=EdgeType.REFERENCES,
            depth=1,
        )
    )

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.document.id == _id("doc_b")
    assert node.edge_counts == {"references": 3}
    # Most recent edge (edge_ref_2) should be shown
    assert node.edge.id == _eid("edge_ref_2")
    assert node.edge.rationale == "Rationale 2"


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
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    now = datetime.now(timezone.utc)
    # 2 supersedes + 3 covers edges from doc_a to doc_b
    for i in range(2):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_sup_{i}"),
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.SUPERSEDES,
                created_at=now + timedelta(seconds=i),
            )
        )
    for i in range(3):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_cov_{i}"),
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.COVERS,
                resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_b"),
                created_at=now + timedelta(seconds=i),
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
    assert node.edge_counts == {"supersedes": 2, "covers": 3}


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
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    now = datetime.now(timezone.utc)
    for i in range(2):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_sup_{i}"),
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.SUPERSEDES,
                created_at=now + timedelta(seconds=i),
            )
        )
    for i in range(3):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_cov_{i}"),
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.COVERS,
                created_at=now + timedelta(seconds=i),
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
    assert result.nodes[0].edge_counts == {"supersedes": 2}
    assert "covers" not in result.nodes[0].edge_counts


# ---------------------------------------------------------------------------
# BH-100: Multi-depth traversal with per-node edge_counts
# ---------------------------------------------------------------------------


async def test_bh_100_edge_counts_multi_depth(graph_store, graph_ops_service):
    for name in ["doc_a", "doc_b", "doc_c"]:
        await graph_store.insert_document(_make_doc(_id(name)))

    now = datetime.now(timezone.utc)
    # doc_a -> doc_b: 1 supersedes + 2 covers
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_sup_ab"),
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.SUPERSEDES,
            created_at=now,
        )
    )
    for i in range(2):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_cov_ab_{i}"),
                source_id=_id("doc_a"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.COVERS,
                resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_b"),
                created_at=now + timedelta(seconds=i),
            )
        )

    # doc_b -> doc_c: 3 references
    for i in range(3):
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"edge_ref_bc_{i}"),
                source_id=_id("doc_b"),
                target_id=_id("doc_c"),
                edge_type=EdgeType.REFERENCES,
                resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
                source_valid_from_version=_id("doc_b"),
                target_valid_from_version=_id("doc_c"),
                created_at=now + timedelta(seconds=i),
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
    assert nodes_by_id[_id("doc_b")].edge_counts == {"supersedes": 1, "covers": 2}
    assert nodes_by_id[_id("doc_c")].edge_counts == {"references": 3}


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
    assert result == {"deleted": True, "edge_id": edge.id}

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
