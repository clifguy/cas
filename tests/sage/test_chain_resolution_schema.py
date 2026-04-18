"""Chain-resolution write-time invariant tests (CAS-ADR-017, Chunk 2).

Covers TEST-SAGE-CR-001..012 where the assertion is reachable from the
Chunk-2 surface area (Pydantic, edge-type registry, policy-keyed
invariant in `GraphOpsService.link`). Anchor-in-lineage checks (CR-004)
and retracted_edge_id existence checks (CR-009) require accessors that
land in Chunks 4 and 5 respectively and are marked skipped here with a
pointer to the owning chunk.
"""

from datetime import datetime, timezone

import pytest

from sage.api.errors import (
    EdgeAnchorPolicyViolationError,
    RetractTargetNotEdgeError,
    TBDPolicyEdgeError,
)
from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, LinkRequest


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=f"hash_{doc_id}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _seed(graph_store, *doc_ids: str) -> None:
    for doc_id in doc_ids:
        await graph_store.insert_document(_make_doc(doc_id))


# ---------------------------------------------------------------------------
# CR-001: transitive_both valid — accepted
# ---------------------------------------------------------------------------

async def test_cr_001_transitive_both_valid(graph_store, graph_ops_service):
    await _seed(graph_store, "a3", "b2")

    edge = await graph_ops_service.link(LinkRequest(
        source_id="a3",
        target_id="b2",
        edge_type=EdgeType.COVERS,
        source_valid_from_version="a3",
        target_valid_from_version="b2",
    ))

    assert edge.resolution_policy == ResolutionPolicy.TRANSITIVE_BOTH
    assert edge.source_valid_from_version == "a3"
    assert edge.target_valid_from_version == "b2"
    assert edge.valid_until_version is None
    assert edge.retracted_edge_id is None


# ---------------------------------------------------------------------------
# CR-002: transitive_both missing source anchor — rejected
# ---------------------------------------------------------------------------

async def test_cr_002_transitive_both_missing_source_anchor(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a3", "b2")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a3",
            target_id="b2",
            edge_type=EdgeType.COVERS,
            target_valid_from_version="b2",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert exc_info.value.status_code == 400
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-003: transitive_both missing target anchor — rejected
# ---------------------------------------------------------------------------

async def test_cr_003_transitive_both_missing_target_anchor(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a3", "b2")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a3",
            target_id="b2",
            edge_type=EdgeType.COVERS,
            source_valid_from_version="a3",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-004: source anchor outside lineage — rejected (Chunk 4)
# ---------------------------------------------------------------------------

async def test_cr_004_source_anchor_outside_lineage(
    graph_store, graph_ops_service
):
    # Chain: a3 is the oldest, a5 is the newest (a4 supersedes a3, a5
    # supersedes a4). source_id=a3's lineage is just {a3}; an anchor of
    # a5 (a descendant, not an ancestor) is NOT in its lineage.
    await _seed(graph_store, "a3", "a4", "a5", "b2")
    now = datetime.now(timezone.utc)
    await graph_store.insert_edge(Edge(
        id="sup_a4_a3",
        source_id="a4",
        target_id="a3",
        edge_type=EdgeType.SUPERSEDES,
        resolution_policy=ResolutionPolicy.NONE,
        created_at=now,
    ))
    await graph_store.insert_edge(Edge(
        id="sup_a5_a4",
        source_id="a5",
        target_id="a4",
        edge_type=EdgeType.SUPERSEDES,
        resolution_policy=ResolutionPolicy.NONE,
        created_at=now,
    ))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a3",
            target_id="b2",
            edge_type=EdgeType.COVERS,
            source_valid_from_version="a5",
            target_valid_from_version="b2",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-005: transitive_source valid (target anchor copied from target_id)
# ---------------------------------------------------------------------------

async def test_cr_005_transitive_source_target_anchor_copied(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "patent_v3", "uspto_template_v2")

    edge = await graph_ops_service.link(LinkRequest(
        source_id="patent_v3",
        target_id="uspto_template_v2",
        edge_type=EdgeType.DERIVED_FROM,
        source_valid_from_version="patent_v3",
    ))

    assert edge.resolution_policy == ResolutionPolicy.TRANSITIVE_SOURCE
    assert edge.source_valid_from_version == "patent_v3"
    assert edge.target_valid_from_version == "uspto_template_v2"


# ---------------------------------------------------------------------------
# CR-006: transitive_source with explicit target anchor — rejected
# ---------------------------------------------------------------------------

async def test_cr_006_transitive_source_explicit_target_anchor_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "patent_v3", "uspto_template_v2")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="patent_v3",
            target_id="uspto_template_v2",
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version="patent_v3",
            target_valid_from_version="uspto_template_v1",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-007: policy=none (supersedes) with anchors supplied — rejected
# ---------------------------------------------------------------------------

async def test_cr_007_supersedes_with_anchors_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a5", "a4")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a5",
            target_id="a4",
            edge_type=EdgeType.SUPERSEDES,
            source_valid_from_version="a5",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert exc_info.value.detail["resolution_policy"] == "none"
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-008: retracts edge shape (no target_id, retracted_edge_id + source anchor)
# ---------------------------------------------------------------------------

async def test_cr_008_retracts_shape_accepted(graph_store, graph_ops_service):
    await _seed(graph_store, "a3", "b2", "a7")
    covers = await graph_ops_service.link(LinkRequest(
        source_id="a3",
        target_id="b2",
        edge_type=EdgeType.COVERS,
        source_valid_from_version="a3",
        target_valid_from_version="b2",
    ))

    edge = await graph_ops_service.link(LinkRequest(
        source_id="a7",
        target_id=None,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=covers.id,
        source_valid_from_version="a7",
    ))

    assert edge.resolution_policy == ResolutionPolicy.NONE
    assert edge.target_id is None
    assert edge.retracted_edge_id == covers.id
    assert edge.source_valid_from_version == "a7"
    assert edge.target_valid_from_version is None


# ---------------------------------------------------------------------------
# CR-009: retracts with unknown retracted_edge_id — rejected (Chunk 5)
# ---------------------------------------------------------------------------

async def test_cr_009_retracts_unknown_target_edge(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a7")

    with pytest.raises(RetractTargetNotEdgeError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a7",
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id="does-not-exist",
            source_valid_from_version="a7",
        ))
    assert exc_info.value.code == "retract_target_not_edge"
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["retracted_edge_id"] == "does-not-exist"


# ---------------------------------------------------------------------------
# CR-010: TBD-policy edge — rejected
# ---------------------------------------------------------------------------

async def test_cr_010_tbd_policy_edge_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, "d1", "d2")

    with pytest.raises(TBDPolicyEdgeError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="d1",
            target_id="d2",
            edge_type=EdgeType.AUTHORITATIVE_FOR,
            source_valid_from_version="d1",
            target_valid_from_version="d2",
        ))
    assert exc_info.value.code == "tbd_policy_edge"
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# CR-011: non-retracts edge with target_id omitted — rejected
# ---------------------------------------------------------------------------

async def test_cr_011_non_retracts_missing_target_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a3")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a3",
            target_id=None,
            edge_type=EdgeType.COVERS,
            source_valid_from_version="a3",
        ))
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_id" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-012: resolution_policy frozen at creation (round-trip via DB)
# ---------------------------------------------------------------------------

async def test_cr_012_resolution_policy_frozen_on_row(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a3", "b2")

    created = await graph_ops_service.link(LinkRequest(
        source_id="a3",
        target_id="b2",
        edge_type=EdgeType.COVERS,
        source_valid_from_version="a3",
        target_valid_from_version="b2",
    ))

    # Round-trip through the DB: the policy column is populated and
    # re-reads as TRANSITIVE_BOTH regardless of any in-memory registry
    # state.
    read_back = await graph_store.get_edge(created.id)
    assert read_back is not None
    assert read_back.resolution_policy == ResolutionPolicy.TRANSITIVE_BOTH
    assert read_back.source_valid_from_version == "a3"
    assert read_back.target_valid_from_version == "b2"


# ---------------------------------------------------------------------------
# Extra: retracts edge with retracted_edge_id missing — rejected (shape)
# ---------------------------------------------------------------------------

async def test_retracts_missing_retracted_edge_id_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a7")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a7",
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version="a7",
        ))
    assert "retracted_edge_id" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# Extra: retracts edge missing source anchor — rejected (shape)
# ---------------------------------------------------------------------------

async def test_retracts_missing_source_anchor_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, "a7")

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(LinkRequest(
            source_id="a7",
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id="some-edge-id",
        ))
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]
