"""Chain-resolution write-time invariant tests (CAS-ADR-017, Chunk 2).

Covers TEST-SAGE-CR-001..012 where the assertion is reachable from the
Chunk-2 surface area (Pydantic, edge-type registry, policy-keyed
invariant in `GraphOpsService.link`). Anchor-in-lineage checks (CR-004)
and retracted_edge_id existence checks (CR-009) require accessors that
land in Chunks 4 and 5 respectively and are marked skipped here with a
pointer to the owning chunk.
"""

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from sage.api.errors import (
    EdgeAnchorPolicyViolationError,
    RetractTargetNotEdgeError,
    TBDPolicyEdgeError,
)
from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, LinkRequest


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid LinkRequest / TraverseRequest / ChainRequest
    instances. Deterministic — the same name always yields the same id.
    """
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


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
    await _seed(graph_store, _id("a3"), _id("b2"))

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )

    assert edge.resolution_policy == ResolutionPolicy.TRANSITIVE_BOTH
    assert edge.source_valid_from_version == _id("a3")
    assert edge.target_valid_from_version == _id("b2")
    assert edge.valid_until_version is None
    assert edge.retracted_edge_id is None


# ---------------------------------------------------------------------------
# CR-002: transitive_both missing source anchor — rejected
# ---------------------------------------------------------------------------


async def test_cr_002_transitive_both_missing_source_anchor(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a3"), _id("b2"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.COVERS,
                target_valid_from_version=_id("b2"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert exc_info.value.status_code == 400
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-003: transitive_both missing target anchor — rejected
# ---------------------------------------------------------------------------


async def test_cr_003_transitive_both_missing_target_anchor(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a3"), _id("b2"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.COVERS,
                source_valid_from_version=_id("a3"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-004: source anchor outside lineage — rejected (Chunk 4)
# ---------------------------------------------------------------------------


async def test_cr_004_source_anchor_outside_lineage(graph_store, graph_ops_service):
    # Chain: a3 is the oldest, a5 is the newest (a4 supersedes a3, a5
    # supersedes a4). source_id=a3's lineage is just {a3}; an anchor of
    # a5 (a descendant, not an ancestor) is NOT in its lineage.
    await _seed(graph_store, _id("a3"), _id("a4"), _id("a5"), _id("b2"))
    now = datetime.now(timezone.utc)
    await graph_store.insert_edge(
        Edge(
            id=str(uuid.uuid4()),
            source_id=_id("a4"),
            target_id=_id("a3"),
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=now,
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=str(uuid.uuid4()),
            source_id=_id("a5"),
            target_id=_id("a4"),
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=now,
        )
    )

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.COVERS,
                source_valid_from_version=_id("a5"),
                target_valid_from_version=_id("b2"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-005: transitive_source valid; stored target_valid_from_version is null
# (null-means-not-applicable per CAS-ADR-017).
# ---------------------------------------------------------------------------


async def test_cr_005_transitive_source_stores_null_target_anchor(graph_store, graph_ops_service):
    await _seed(graph_store, _id("patent_v3"), _id("uspto_template_v2"))

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("patent_v3"),
            target_id=_id("uspto_template_v2"),
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version=_id("patent_v3"),
        )
    )

    assert edge.resolution_policy == ResolutionPolicy.TRANSITIVE_SOURCE
    assert edge.source_valid_from_version == _id("patent_v3")
    assert edge.target_valid_from_version is None

    # Round-trip through the DB confirms the null is persisted, not just
    # a Pydantic default on the in-memory instance.
    read_back = await graph_store.get_edge(edge.id)
    assert read_back is not None
    assert read_back.target_valid_from_version is None


# ---------------------------------------------------------------------------
# CR-006: transitive_source with explicit target anchor — rejected
# ---------------------------------------------------------------------------


async def test_cr_006_transitive_source_explicit_target_anchor_rejected(
    graph_store, graph_ops_service
):
    await _seed(graph_store, _id("patent_v3"), _id("uspto_template_v2"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("patent_v3"),
                target_id=_id("uspto_template_v2"),
                edge_type=EdgeType.DERIVED_FROM,
                source_valid_from_version=_id("patent_v3"),
                target_valid_from_version=_id("uspto_template_v1"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-007: policy=none (supersedes) with anchors supplied — rejected
# ---------------------------------------------------------------------------


async def test_cr_007_supersedes_with_anchors_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a5"), _id("a4"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a5"),
                target_id=_id("a4"),
                edge_type=EdgeType.SUPERSEDES,
                source_valid_from_version=_id("a5"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert exc_info.value.detail["resolution_policy"] == "none"
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-008: retracts edge shape (no target_id, retracted_edge_id + source anchor)
# ---------------------------------------------------------------------------


async def test_cr_008_retracts_shape_accepted(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a3"), _id("b2"), _id("a7"))
    covers = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a7"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id=covers.id,
            source_valid_from_version=_id("a7"),
        )
    )

    assert edge.resolution_policy == ResolutionPolicy.NONE
    assert edge.target_id is None
    assert edge.retracted_edge_id == covers.id
    assert edge.source_valid_from_version == _id("a7")
    assert edge.target_valid_from_version is None


# ---------------------------------------------------------------------------
# CR-009: retracts with unknown retracted_edge_id — rejected (Chunk 5)
# ---------------------------------------------------------------------------


async def test_cr_009_retracts_unknown_target_edge(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a7"))

    # Use a valid-shape UUID that doesn't exist in the store; the runtime
    # check inside graph_ops then surfaces RetractTargetNotEdgeError. A
    # malformed-shape value would short-circuit at LinkRequest validation.
    bogus_edge_id = str(uuid.uuid4())
    with pytest.raises(RetractTargetNotEdgeError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a7"),
                target_id=None,
                edge_type=EdgeType.RETRACTS,
                retracted_edge_id=bogus_edge_id,
                source_valid_from_version=_id("a7"),
            )
        )
    assert exc_info.value.code == "retract_target_not_edge"
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["retracted_edge_id"] == bogus_edge_id


# ---------------------------------------------------------------------------
# CR-010: TBD-policy edge — rejected
# ---------------------------------------------------------------------------


async def test_cr_010_tbd_policy_edge_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, _id("d1"), _id("d2"))

    with pytest.raises(TBDPolicyEdgeError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("d1"),
                target_id=_id("d2"),
                edge_type=EdgeType.AUTHORITATIVE_FOR,
                source_valid_from_version=_id("d1"),
                target_valid_from_version=_id("d2"),
            )
        )
    assert exc_info.value.code == "tbd_policy_edge"
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# CR-011: non-retracts edge with target_id omitted — rejected
# ---------------------------------------------------------------------------


async def test_cr_011_non_retracts_missing_target_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a3"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=None,
                edge_type=EdgeType.COVERS,
                source_valid_from_version=_id("a3"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_id" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-012: resolution_policy frozen at creation (round-trip via DB)
# ---------------------------------------------------------------------------


async def test_cr_012_resolution_policy_frozen_on_row(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a3"), _id("b2"))

    created = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )

    # Round-trip through the DB: the policy column is populated and
    # re-reads as TRANSITIVE_BOTH regardless of any in-memory registry
    # state.
    read_back = await graph_store.get_edge(created.id)
    assert read_back is not None
    assert read_back.resolution_policy == ResolutionPolicy.TRANSITIVE_BOTH
    assert read_back.source_valid_from_version == _id("a3")
    assert read_back.target_valid_from_version == _id("b2")


# ---------------------------------------------------------------------------
# Extra: retracts edge with retracted_edge_id missing — rejected (shape)
# ---------------------------------------------------------------------------


async def test_retracts_missing_retracted_edge_id_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a7"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a7"),
                target_id=None,
                edge_type=EdgeType.RETRACTS,
                source_valid_from_version=_id("a7"),
            )
        )
    assert "retracted_edge_id" in exc_info.value.detail["offending_fields"]


# ===========================================================================
# Section 7: transitive_target (mirror of transitive_source)
#
# No built-in edge type maps to transitive_target in the frozen 11-row
# registry. These tests construct a custom EdgeTypeRegistry that maps
# AUTHORITATIVE_FOR -> TRANSITIVE_TARGET so the policy paths are exercised.
# ===========================================================================

from sage.config import VaultConfig  # noqa: E402
from sage.models.edge_registry import EdgeTypeRegistry  # noqa: E402
from sage.services.graph_ops import GraphOpsService  # noqa: E402


def _transitive_target_registry() -> EdgeTypeRegistry:
    policies = {
        EdgeType.SUPERSEDES: ResolutionPolicy.NONE,
        EdgeType.RETRACTS: ResolutionPolicy.NONE,
        EdgeType.MERGED_FROM: ResolutionPolicy.NONE,
        EdgeType.DERIVED_FROM: ResolutionPolicy.TRANSITIVE_SOURCE,
        EdgeType.INSTANTIATED_FROM: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.REFERENCES: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.COVERS: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.BUNDLES_WITH: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.DEPENDS_ON: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.AUTHORITATIVE_FOR: ResolutionPolicy.TRANSITIVE_TARGET,
        EdgeType.SYNC_TARGET: ResolutionPolicy.TBD,
    }
    return EdgeTypeRegistry(policies)


def _tt_service(graph_store, minimal_config: VaultConfig) -> GraphOpsService:
    return GraphOpsService(
        graph_store, minimal_config, edge_type_registry=_transitive_target_registry()
    )


# ---------------------------------------------------------------------------
# CR-046: transitive_target valid — accepted
# ---------------------------------------------------------------------------


async def test_cr_046_transitive_target_valid(graph_store, minimal_config):
    service = _tt_service(graph_store, minimal_config)
    await _seed(graph_store, _id("a3"), _id("b1"), _id("b2"))
    # Target chain: b1 <- b2
    from sage.models.schemas import Edge as _Edge

    await graph_store.insert_edge(
        _Edge(
            id=str(uuid.uuid4()),
            source_id=_id("b2"),
            target_id=_id("b1"),
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=datetime.now(timezone.utc),
        )
    )

    edge = await service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.AUTHORITATIVE_FOR,
            target_valid_from_version=_id("b2"),
        )
    )

    assert edge.resolution_policy == ResolutionPolicy.TRANSITIVE_TARGET
    assert edge.source_valid_from_version is None
    assert edge.target_valid_from_version == _id("b2")

    # Round-trip confirms null source anchor persists.
    read_back = await graph_store.get_edge(edge.id)
    assert read_back is not None
    assert read_back.resolution_policy == ResolutionPolicy.TRANSITIVE_TARGET
    assert read_back.source_valid_from_version is None
    assert read_back.target_valid_from_version == _id("b2")


# ---------------------------------------------------------------------------
# CR-047: transitive_target missing target anchor — rejected
# ---------------------------------------------------------------------------


async def test_cr_047_transitive_target_missing_target_anchor(graph_store, minimal_config):
    service = _tt_service(graph_store, minimal_config)
    await _seed(graph_store, _id("a3"), _id("b2"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.AUTHORITATIVE_FOR,
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert exc_info.value.detail["resolution_policy"] == "transitive_target"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-048: transitive_target with source anchor supplied — rejected
# ---------------------------------------------------------------------------


async def test_cr_048_transitive_target_with_source_anchor_rejected(graph_store, minimal_config):
    service = _tt_service(graph_store, minimal_config)
    await _seed(graph_store, _id("a3"), _id("b2"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.AUTHORITATIVE_FOR,
                source_valid_from_version=_id("a3"),
                target_valid_from_version=_id("b2"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# CR-049: transitive_target with target anchor outside target chain — rejected
# ---------------------------------------------------------------------------


async def test_cr_049_transitive_target_anchor_outside_target_chain(graph_store, minimal_config):
    service = _tt_service(graph_store, minimal_config)
    await _seed(graph_store, _id("a3"), _id("b1"), _id("b2"), _id("c1"))

    # b2 supersedes b1 only. c1 is on a different chain.
    from sage.models.schemas import Edge as _Edge

    await graph_store.insert_edge(
        _Edge(
            id=str(uuid.uuid4()),
            source_id=_id("b2"),
            target_id=_id("b1"),
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=datetime.now(timezone.utc),
        )
    )

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await service.link(
            LinkRequest(
                source_id=_id("a3"),
                target_id=_id("b2"),
                edge_type=EdgeType.AUTHORITATIVE_FOR,
                target_valid_from_version=_id("c1"),
            )
        )
    assert exc_info.value.code == "edge_anchor_policy_violation"
    assert "target_valid_from_version" in exc_info.value.detail["offending_fields"]


# ---------------------------------------------------------------------------
# Extra: retracts edge missing source anchor — rejected (shape)
# ---------------------------------------------------------------------------


async def test_retracts_missing_source_anchor_rejected(graph_store, graph_ops_service):
    await _seed(graph_store, _id("a7"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as exc_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("a7"),
                target_id=None,
                edge_type=EdgeType.RETRACTS,
                retracted_edge_id=str(uuid.uuid4()),
            )
        )
    assert "source_valid_from_version" in exc_info.value.detail["offending_fields"]
