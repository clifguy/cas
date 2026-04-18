"""Chain-resolution traversal tests (CAS-ADR-017, Chunk 4).

Covers TEST-SAGE-CR-013..022: sage_traverse honors the edge-type
resolution_policy registry and anchor-in-lineage filtering. All tests
use the canonical ADR worked example unless otherwise noted:

    Chain A: a1 <- a2 <- a3 <- a4 <- a5   (source=newer, target=older)
    Chain B: b1 <- b2 <- b3
    covers edge: source=a3, target=b2, policy=transitive_both,
                 source_anchor=a3, target_anchor=b2.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import Document, Edge, LinkRequest, TraverseRequest


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


async def _seed_docs(graph_store, *doc_ids: str) -> None:
    for doc_id in doc_ids:
        await graph_store.insert_document(_make_doc(doc_id))


async def _seed_supersedes_chain(graph_store, chain: list[str]) -> None:
    """Insert supersedes edges connecting chain oldest->newest.

    For chain [a1, a2, a3, a4, a5], writes edges
        a2->a1, a3->a2, a4->a3, a5->a4  (source supersedes target).
    """
    now = datetime.now(timezone.utc)
    for i in range(1, len(chain)):
        newer, older = chain[i], chain[i - 1]
        await graph_store.insert_edge(Edge(
            id=f"sup_{newer}_{older}",
            source_id=newer,
            target_id=older,
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=now + timedelta(seconds=i),
        ))


async def _seed_ab_worked_example(graph_store, graph_ops_service):
    """Seed the canonical ADR example: chains A/B + covers edge at a3/b2."""
    chain_a = ["a1", "a2", "a3", "a4", "a5"]
    chain_b = ["b1", "b2", "b3"]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    await graph_ops_service.link(LinkRequest(
        source_id="a3",
        target_id="b2",
        edge_type=EdgeType.COVERS,
        source_valid_from_version="a3",
        target_valid_from_version="b2",
    ))


# ---------------------------------------------------------------------------
# CR-013: query from (a5, b3) surfaces the covers edge
# ---------------------------------------------------------------------------

async def test_cr_013_covers_surfaces_from_chain_heads(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="a5",
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in out.nodes] == ["b2"]

    inbound = await graph_ops_service.traverse(TraverseRequest(
        start_id="b3",
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.INBOUND,
        depth=2,
    ))
    assert [n.document.id for n in inbound.nodes] == ["a3"]


# ---------------------------------------------------------------------------
# CR-014: query from (a2, b3) suppresses the covers edge (a3 not in a2's lineage)
# ---------------------------------------------------------------------------

async def test_cr_014_covers_suppressed_when_source_anchor_ahead_of_start(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="a2",
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out.nodes == []


# ---------------------------------------------------------------------------
# CR-015: query from (_, b1) suppresses the covers edge (b2 not in b1's lineage)
# ---------------------------------------------------------------------------

async def test_cr_015_covers_suppressed_when_target_anchor_ahead_of_start(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    inbound = await graph_ops_service.traverse(TraverseRequest(
        start_id="b1",
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.INBOUND,
        depth=2,
    ))
    assert inbound.nodes == []


# ---------------------------------------------------------------------------
# CR-016: anchors exactly match start — inclusive hit
# ---------------------------------------------------------------------------

async def test_cr_016_anchors_exactly_match_start(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="a3",
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in out.nodes] == ["b2"]


# ---------------------------------------------------------------------------
# CR-017: policy=none (supersedes) traverses without anchor filtering
# ---------------------------------------------------------------------------

async def test_cr_017_supersedes_ignores_anchor_logic(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="a5",
        edge_type=EdgeType.SUPERSEDES,
        direction=TraversalDirection.OUTBOUND,
        depth=5,
    ))
    assert {n.document.id for n in out.nodes} == {"a1", "a2", "a3", "a4"}


# ---------------------------------------------------------------------------
# CR-018: transitive_source — source anchor in lineage — surfaces
# ---------------------------------------------------------------------------

async def test_cr_018_transitive_source_source_anchor_in_lineage(
    graph_store, graph_ops_service
):
    await _seed_docs(
        graph_store,
        "patent_v1", "patent_v2", "patent_v3", "patent_v4",
        "uspto_template_v1", "uspto_template_v2",
    )
    await _seed_supersedes_chain(
        graph_store,
        ["patent_v1", "patent_v2", "patent_v3", "patent_v4"],
    )
    await _seed_supersedes_chain(
        graph_store,
        ["uspto_template_v1", "uspto_template_v2"],
    )
    await graph_ops_service.link(LinkRequest(
        source_id="patent_v3",
        target_id="uspto_template_v2",
        edge_type=EdgeType.DERIVED_FROM,
        source_valid_from_version="patent_v3",
    ))

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="patent_v4",
        edge_type=EdgeType.DERIVED_FROM,
        direction=TraversalDirection.OUTBOUND,
        depth=1,
    ))
    assert [n.document.id for n in out.nodes] == ["uspto_template_v2"]


# ---------------------------------------------------------------------------
# CR-019: transitive_source — target chain advances; edge still points at
# frozen target.
# ---------------------------------------------------------------------------

async def test_cr_019_transitive_source_target_frozen_after_target_chain_advances(
    graph_store, graph_ops_service
):
    await _seed_docs(
        graph_store,
        "patent_v1", "patent_v2", "patent_v3", "patent_v4",
        "uspto_template_v1", "uspto_template_v2", "uspto_template_v3",
    )
    await _seed_supersedes_chain(
        graph_store,
        ["patent_v1", "patent_v2", "patent_v3", "patent_v4"],
    )
    await _seed_supersedes_chain(
        graph_store,
        ["uspto_template_v1", "uspto_template_v2", "uspto_template_v3"],
    )
    await graph_ops_service.link(LinkRequest(
        source_id="patent_v3",
        target_id="uspto_template_v2",
        edge_type=EdgeType.DERIVED_FROM,
        source_valid_from_version="patent_v3",
    ))

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="patent_v4",
        edge_type=EdgeType.DERIVED_FROM,
        direction=TraversalDirection.OUTBOUND,
        depth=1,
    ))
    assert len(out.nodes) == 1
    node = out.nodes[0]
    assert node.document.id == "uspto_template_v2"
    assert node.edge.target_id == "uspto_template_v2"


# ---------------------------------------------------------------------------
# CR-020: mixed traverse without edge_type filter honors per-edge policy
# ---------------------------------------------------------------------------

async def test_cr_020_mixed_traverse_per_edge_policy(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id="a2",
        direction=TraversalDirection.OUTBOUND,
        depth=3,
    ))
    # supersedes surfaces (policy none); covers does NOT (anchor a3 > a2).
    found = {n.document.id for n in out.nodes}
    assert "a1" in found  # reached via supersedes a2->a1
    assert "b2" not in found  # covers would require anchor a3 in a2 lineage


# ---------------------------------------------------------------------------
# CR-021: anchor document purged — conservative suppress + WARN log
# ---------------------------------------------------------------------------

async def test_cr_021_anchor_document_missing_suppresses_with_warn(
    graph_store, graph_ops_service, caplog
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    # Purge a3 (the source anchor) directly from the documents table.
    # This simulates the rare repair scenario covered by CR-021 without
    # requiring a public delete_document helper.
    def _purge(doc_id: str) -> None:
        conn = graph_store._get_connection()
        conn.execute("PRAGMA foreign_keys=OFF;")
        try:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON;")

    await graph_store._run(_purge, "a3")

    with caplog.at_level(logging.WARNING, logger="sage.services.graph_ops"):
        out = await graph_ops_service.traverse(TraverseRequest(
            start_id="a5",
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
        ))

    assert out.nodes == []
    assert any(
        "a3" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# CR-022: per-request lineage cache coalesces repeated lookups
# ---------------------------------------------------------------------------

async def test_cr_022_per_request_lineage_cache_coalesces_lookups(
    graph_store, graph_ops_service
):
    """Wrap get_supersedes_lineage to count invocations across a traverse.

    With 5 covers edges all anchored on Chain A's existing members, the
    resolver should fetch each distinct endpoint's lineage at most once.
    """
    chain_a = ["a1", "a2", "a3", "a4", "a5"]
    chain_b = ["b1", "b2", "b3"]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    # Five covers edges anchored at various points on both chains.
    for src, tgt in [
        ("a2", "b1"), ("a3", "b1"), ("a3", "b2"), ("a4", "b2"), ("a4", "b3"),
    ]:
        await graph_ops_service.link(LinkRequest(
            source_id=src,
            target_id=tgt,
            edge_type=EdgeType.COVERS,
            source_valid_from_version=src,
            target_valid_from_version=tgt,
        ))

    call_count = {"n": 0}
    original = graph_store.get_supersedes_lineage

    async def counting(doc_id: str) -> list[str]:
        call_count["n"] += 1
        return await original(doc_id)

    graph_store.get_supersedes_lineage = counting
    try:
        await graph_ops_service.traverse(TraverseRequest(
            start_id="a5",
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
        ))
    finally:
        graph_store.get_supersedes_lineage = original

    # 5 seeds lineage-expanded from a5 + per-edge anchor checks for at
    # most 5 distinct endpoints on each side. A naive implementation
    # would call lineage O(edges * 2) times; with caching it must stay
    # bounded by the number of distinct doc_ids touched (<= 8 for this
    # graph: a1..a5, b1..b3). The exact upper bound is the number of
    # distinct endpoint doc_ids referenced.
    assert call_count["n"] <= 8


# ===========================================================================
# Section: transitive_target (mirror of transitive_source)
#
# The built-in registry has no transitive_target edge types. These tests
# construct a custom EdgeTypeRegistry mapping AUTHORITATIVE_FOR to
# transitive_target so the traversal paths (seed determination, anchor
# filter) are exercised.
# ===========================================================================

from sage.models.edge_registry import EdgeTypeRegistry  # noqa: E402
from sage.services.graph_ops import GraphOpsService  # noqa: E402


def _tt_registry() -> EdgeTypeRegistry:
    return EdgeTypeRegistry({
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
    })


async def _seed_transitive_target_example(graph_store, service) -> None:
    """Source chain a1..a5 (a3 is the frozen source); target chain b1..b3.

    Edge: source=a3, target=b2, target_anchor=b2, policy=transitive_target.
    """
    await _seed_docs(
        graph_store, "a1", "a2", "a3", "a4", "a5", "b1", "b2", "b3"
    )
    await _seed_supersedes_chain(graph_store, ["a1", "a2", "a3", "a4", "a5"])
    await _seed_supersedes_chain(graph_store, ["b1", "b2", "b3"])
    await service.link(LinkRequest(
        source_id="a3",
        target_id="b2",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        target_valid_from_version="b2",
    ))


# ---------------------------------------------------------------------------
# CR-050: transitive_target — target anchor in lineage — surfaces (both dirs)
# ---------------------------------------------------------------------------

async def test_cr_050_transitive_target_target_anchor_in_lineage_surfaces(
    graph_store, minimal_config
):
    service = GraphOpsService(
        graph_store, minimal_config, edge_type_registry=_tt_registry()
    )
    await _seed_transitive_target_example(graph_store, service)

    # Outbound from the frozen source a3: seeds = [a3].
    out = await service.traverse(TraverseRequest(
        start_id="a3",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in out.nodes] == ["b2"]

    # Inbound from b3 (downstream of anchor b2): seeds = b3 lineage.
    inbound = await service.traverse(TraverseRequest(
        start_id="b3",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.INBOUND,
        depth=2,
    ))
    assert [n.document.id for n in inbound.nodes] == ["a3"]


# ---------------------------------------------------------------------------
# CR-051: transitive_target — target anchor not in lineage — suppressed
# ---------------------------------------------------------------------------

async def test_cr_051_transitive_target_target_anchor_not_in_lineage_suppressed(
    graph_store, minimal_config
):
    service = GraphOpsService(
        graph_store, minimal_config, edge_type_registry=_tt_registry()
    )
    await _seed_transitive_target_example(graph_store, service)

    # b1 is upstream of the anchor b2: anchor check fails.
    out = await service.traverse(TraverseRequest(
        start_id="b1",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.INBOUND,
        depth=2,
    ))
    assert out.nodes == []


# ---------------------------------------------------------------------------
# CR-052: transitive_target — source chain advance is NOT seed-expanded
# (source endpoint is frozen at derivation).
# ---------------------------------------------------------------------------

async def test_cr_052_transitive_target_source_frozen_not_seed_expanded(
    graph_store, minimal_config
):
    service = GraphOpsService(
        graph_store, minimal_config, edge_type_registry=_tt_registry()
    )
    await _seed_transitive_target_example(graph_store, service)

    # a2 is an ancestor of a3 (the frozen source) on the source chain.
    # Because the source endpoint is frozen, outbound seeds from a2 are
    # exactly [a2], and no edge has source_id a2.
    out = await service.traverse(TraverseRequest(
        start_id="a2",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out.nodes == []

    # a4 is downstream of a3 on the source chain. Also must not surface:
    # source is frozen at a3, not live-tracking.
    out4 = await service.traverse(TraverseRequest(
        start_id="a4",
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out4.nodes == []
