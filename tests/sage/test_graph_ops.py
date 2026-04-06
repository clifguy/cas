"""Graph Operations tests: BH-021, BH-023, BH-031 through BH-037.

Covers link (edge creation), check_preconditions (dependency validation),
traverse (graph walk with deduplication), and discover deterministic stub.
"""

import pytest
from datetime import datetime, timedelta, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    PipelineIncompleteError,
    SelfReferentialEdgeError,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, Edge, LinkRequest, TraverseRequest


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
        source_content_hash=f"hash_{doc_id}",
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

async def test_bh_021_failed_doc_excluded_from_deterministic(
    graph_store, graph_ops_service
):
    doc = _make_doc("doc_failed", pipeline_status=PipelineStatus.FAILED)
    doc.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc)

    with pytest.raises(PipelineIncompleteError) as exc_info:
        await graph_ops_service.check_pipeline_for_retrieval("doc_failed")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "pipeline_incomplete"


# ---------------------------------------------------------------------------
# BH-023: Failed document does not satisfy preconditions
# ---------------------------------------------------------------------------

async def test_bh_023_failed_doc_does_not_satisfy_preconditions(
    graph_store, graph_ops_service
):
    doc_function = _make_doc("doc_function")
    doc_dep = _make_doc("doc_dep", pipeline_status=PipelineStatus.FAILED)
    doc_dep.pipeline_error = "indexing failure"
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    # Create depends_on edge
    edge = Edge(
        id="edge_dep_001",
        source_id="doc_function",
        target_id="doc_dep",
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions("doc_function")
    assert result.satisfied is False
    assert len(result.checks) == 1
    assert result.checks[0].target_id == "doc_dep"
    assert result.checks[0].satisfied is False
    assert "pipeline_incomplete" in result.checks[0].actual


# ---------------------------------------------------------------------------
# BH-031: Duplicate edges are permitted
# ---------------------------------------------------------------------------

async def test_bh_031_duplicate_edges_permitted(graph_store, graph_ops_service):
    doc_a = _make_doc("doc_a")
    doc_b = _make_doc("doc_b")
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge1 = await graph_ops_service.link(LinkRequest(
        source_id="doc_a",
        target_id="doc_b",
        edge_type=EdgeType.REFERENCES,
        rationale="First rationale",
    ))
    edge2 = await graph_ops_service.link(LinkRequest(
        source_id="doc_a",
        target_id="doc_b",
        edge_type=EdgeType.REFERENCES,
        rationale="Updated understanding",
    ))

    assert edge1.id != edge2.id

    # Both edges exist in the store
    edges = await graph_store.get_edges_by_source("doc_a", "references")
    assert len(edges) == 2


# ---------------------------------------------------------------------------
# BH-032: Edge records have auto-generated IDs
# ---------------------------------------------------------------------------

async def test_bh_032_edge_auto_generated_id(graph_store, graph_ops_service):
    doc_a = _make_doc("doc_a")
    doc_b = _make_doc("doc_b")
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    edge = await graph_ops_service.link(LinkRequest(
        source_id="doc_a",
        target_id="doc_b",
        edge_type=EdgeType.REFERENCES,
    ))

    assert edge.id
    assert isinstance(edge.id, str)
    assert len(edge.id) > 0


# ---------------------------------------------------------------------------
# BH-033: check_preconditions -- active satisfies dependency
# ---------------------------------------------------------------------------

async def test_bh_033_active_satisfies_dependency(graph_store, graph_ops_service):
    doc_function = _make_doc("doc_function")
    doc_dep = _make_doc("doc_dep", lifecycle_status="active")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id="edge_dep_active",
        source_id="doc_function",
        target_id="doc_dep",
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions("doc_function")
    assert result.satisfied is True
    assert result.checks[0].satisfied is True


# ---------------------------------------------------------------------------
# BH-034: check_preconditions -- completed satisfies dependency
# ---------------------------------------------------------------------------

async def test_bh_034_completed_satisfies_dependency(graph_store, graph_ops_service):
    doc_function = _make_doc("doc_function")
    doc_dep = _make_doc("doc_dep", lifecycle_status="completed")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id="edge_dep_completed",
        source_id="doc_function",
        target_id="doc_dep",
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions("doc_function")
    assert result.satisfied is True


# ---------------------------------------------------------------------------
# BH-035: check_preconditions -- superseded does not satisfy
# ---------------------------------------------------------------------------

async def test_bh_035_superseded_does_not_satisfy(graph_store, graph_ops_service):
    doc_function = _make_doc("doc_function")
    doc_dep = _make_doc("doc_dep", lifecycle_status="superseded")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id="edge_dep_superseded",
        source_id="doc_function",
        target_id="doc_dep",
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await graph_ops_service.check_preconditions("doc_function")
    assert result.satisfied is False
    assert result.checks[0].actual == "superseded"


# ---------------------------------------------------------------------------
# BH-036: check_preconditions -- filed does not satisfy (domain-specific)
# ---------------------------------------------------------------------------

async def test_bh_036_filed_does_not_satisfy(graph_store, pim_graph_ops_service):
    doc_function = _make_doc("doc_function")
    doc_dep = _make_doc("doc_dep", lifecycle_status="filed")
    await graph_store.insert_document(doc_function)
    await graph_store.insert_document(doc_dep)

    edge = Edge(
        id="edge_dep_filed",
        source_id="doc_function",
        target_id="doc_dep",
        edge_type=EdgeType.DEPENDS_ON,
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(edge)

    result = await pim_graph_ops_service.check_preconditions("doc_function")
    assert result.satisfied is False
    assert result.checks[0].actual == "filed"


# ---------------------------------------------------------------------------
# BH-037: Traversal deduplicates by document with edge_count
# ---------------------------------------------------------------------------

async def test_bh_037_traversal_deduplicates_with_edge_count(
    graph_store, graph_ops_service
):
    doc_a = _make_doc("doc_a")
    doc_b = _make_doc("doc_b")
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    base_time = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(3):
        edge = Edge(
            id=f"edge_ref_{i}",
            source_id="doc_a",
            target_id="doc_b",
            edge_type=EdgeType.REFERENCES,
            created_at=base_time + timedelta(hours=i),
            rationale=f"Rationale {i}",
        )
        await graph_store.insert_edge(edge)

    result = await graph_ops_service.traverse(TraverseRequest(
        start_id="doc_a",
        edge_type=EdgeType.REFERENCES,
        depth=1,
    ))

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.document.id == "doc_b"
    assert node.edge_count == 3
    # Most recent edge (edge_ref_2) should be shown
    assert node.edge.id == "edge_ref_2"
    assert node.edge.rationale == "Rationale 2"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

async def test_link_self_referential_raises_400(graph_store, graph_ops_service):
    doc_a = _make_doc("doc_a")
    await graph_store.insert_document(doc_a)

    with pytest.raises(SelfReferentialEdgeError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="doc_a",
            target_id="doc_a",
            edge_type=EdgeType.REFERENCES,
        ))
    assert exc_info.value.status_code == 400


async def test_link_nonexistent_source_raises_404(graph_store, graph_ops_service):
    doc_b = _make_doc("doc_b")
    await graph_store.insert_document(doc_b)

    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.link(LinkRequest(
            source_id="nonexistent",
            target_id="doc_b",
            edge_type=EdgeType.REFERENCES,
        ))


async def test_link_nonexistent_target_raises_404(graph_store, graph_ops_service):
    doc_a = _make_doc("doc_a")
    await graph_store.insert_document(doc_a)

    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.link(LinkRequest(
            source_id="doc_a",
            target_id="nonexistent",
            edge_type=EdgeType.REFERENCES,
        ))


async def test_traverse_nonexistent_start_raises_404(graph_store, graph_ops_service):
    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.traverse(TraverseRequest(start_id="nonexistent"))


async def test_check_preconditions_nonexistent_function_raises_404(
    graph_store, graph_ops_service
):
    with pytest.raises(DocumentNotFoundError):
        await graph_ops_service.check_preconditions("nonexistent")
