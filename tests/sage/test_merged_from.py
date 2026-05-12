"""merged_from write path + tombstoning resolver tests (CAS-ADR-017, Chunk 6).

Covers TEST-SAGE-CR-029..036. Uses the canonical worked example:

    Chain A: a1 <- a2 <- a3 <- a4 <- a5 <- a6 <- a7 <- a8   (a8 is chain head)
    Chain B: b1 <- b2 <- b3 <- b4
    covers:  source=a3, target=b2, policy=transitive_both (anchors a3/b2)

Chain C is added for the merge (c1, then optionally c2 supersedes c1).
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sage.api.errors import (
    EdgeAnchorPolicyViolationError,
    MergedFromValidationError,
)
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import Document, Edge, LinkRequest, TraverseRequest


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid LinkRequest / TraverseRequest / ChainRequest
    instances. Deterministic — the same name always yields the same id.
    """
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


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
    now = datetime.now(timezone.utc)
    for i in range(1, len(chain)):
        newer, older = chain[i], chain[i - 1]
        await graph_store.insert_edge(
            Edge(
                id=_eid(f"sup_{newer}_{older}"),
                source_id=newer,
                target_id=older,
                edge_type=EdgeType.SUPERSEDES,
                resolution_policy=ResolutionPolicy.NONE,
                created_at=now + timedelta(seconds=i),
            )
        )


async def _seed_ab_worked_example(graph_store, graph_ops_service) -> str:
    """Seed Chains A (a1..a8), B (b1..b4), and the canonical covers edge.

    Returns the covers edge id.
    """
    chain_a = [_id(n) for n in ("a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8")]
    chain_b = [_id(n) for n in ("b1", "b2", "b3", "b4")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    covers = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )
    return covers.id


# ---------------------------------------------------------------------------
# CR-029: valid merged_from (chain-first successor, chain-head predecessor)
# ---------------------------------------------------------------------------


async def test_cr_029_merged_from_accepts_chain_first_and_chain_head(
    graph_store, graph_ops_service
):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))

    edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    assert edge.edge_type == EdgeType.MERGED_FROM
    assert edge.resolution_policy == ResolutionPolicy.NONE
    assert edge.source_valid_from_version is None
    assert edge.target_valid_from_version is None
    assert edge.valid_until_version is None

    # And the edge persists.
    stored = await graph_store.get_edge(edge.id)
    assert stored is not None
    assert stored.edge_type == EdgeType.MERGED_FROM


# ---------------------------------------------------------------------------
# CR-030: non-terminal predecessor rejected
# ---------------------------------------------------------------------------


async def test_cr_030_merged_from_rejects_non_terminal_predecessor(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))

    with pytest.raises(MergedFromValidationError) as excinfo:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("c1"),
                target_id=_id("a5"),
                edge_type=EdgeType.MERGED_FROM,
            )
        )

    err = excinfo.value
    assert err.status_code == 400
    assert err.code == "merged_from_validation"
    assert err.detail["target_id"] == _id("a5")
    assert "chain head" in err.detail["violation"]


# ---------------------------------------------------------------------------
# CR-031: non-first successor rejected
# ---------------------------------------------------------------------------


async def test_cr_031_merged_from_rejects_non_first_successor(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    # Build a Chain C with c1 <- c2 so c2 has an outbound supersedes edge.
    await _seed_docs(graph_store, _id("c1"), _id("c2"))
    await _seed_supersedes_chain(graph_store, [_id("c1"), _id("c2")])

    with pytest.raises(MergedFromValidationError) as excinfo:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("c2"),
                target_id=_id("a8"),
                edge_type=EdgeType.MERGED_FROM,
            )
        )

    err = excinfo.value
    assert err.status_code == 400
    assert err.code == "merged_from_validation"
    assert err.detail["source_id"] == _id("c2")
    assert "first version" in err.detail["violation"]


# ---------------------------------------------------------------------------
# CR-032: merged_from atomically tombstones predecessor-chain edges
# ---------------------------------------------------------------------------


async def test_cr_032_merged_from_tombstones_atomically(graph_store, graph_ops_service):
    covers_id = await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))

    # Precondition: covers edge is not tombstoned before the merge.
    covers_before = await graph_store.get_edge(covers_id)
    assert covers_before.valid_until_version is None

    # Supersedes edges on Chain A are policy=none and must NOT be tombstoned.
    sup_edge_id = _eid(f"sup_{_id('a8')}_{_id('a7')}")
    sup_edge_before = await graph_store.get_edge(sup_edge_id)
    assert sup_edge_before.valid_until_version is None

    merged = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    # The covers edge now carries valid_until_version = a8.
    covers_after = await graph_store.get_edge(covers_id)
    assert covers_after.valid_until_version == _id("a8")

    # Policy-none edges on Chain A are untouched (lineage stays navigable).
    sup_edge_after = await graph_store.get_edge(sup_edge_id)
    assert sup_edge_after.valid_until_version is None

    # And the merged_from edge itself carries no tombstone.
    assert merged.valid_until_version is None
    merged_stored = await graph_store.get_edge(merged.id)
    assert merged_stored.valid_until_version is None


# ---------------------------------------------------------------------------
# CR-033: after merge, query from c2 does NOT inherit the covers edge
# ---------------------------------------------------------------------------


async def test_cr_033_query_from_successor_does_not_inherit(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"), _id("c2"))
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )
    # c2 supersedes c1 AFTER the merge.
    await _seed_supersedes_chain(graph_store, [_id("c1"), _id("c2")])

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("c2"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=3,
        )
    )
    assert out.nodes == []


# ---------------------------------------------------------------------------
# CR-034: historical query at the merge boundary still surfaces the edge
# ---------------------------------------------------------------------------


async def test_cr_034_query_at_merge_boundary_surfaces(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a8"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
        )
    )
    # a8 == valid_until_version: tombstone marks the BOUNDARY, not suppress.
    assert [n.document.id for n in out.nodes] == [_id("b2")]


# ---------------------------------------------------------------------------
# CR-035: time-travel query at pre-merge version still surfaces the edge
# ---------------------------------------------------------------------------


async def test_cr_035_time_travel_query_pre_merge_surfaces(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a5"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
        )
    )
    # valid_until_version=a8 is NOT in lineage(a5)={a5,a4,a3,a2,a1}: kept.
    assert [n.document.id for n in out.nodes] == [_id("b2")]


# ---------------------------------------------------------------------------
# CR-036: merged_from with any anchor field -> EDGE_ANCHOR_POLICY_VIOLATION
# ---------------------------------------------------------------------------


async def test_cr_036_merged_from_rejects_anchor_fields(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))

    with pytest.raises(EdgeAnchorPolicyViolationError) as excinfo:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("c1"),
                target_id=_id("a8"),
                edge_type=EdgeType.MERGED_FROM,
                source_valid_from_version=_id("c1"),
            )
        )

    err = excinfo.value
    assert err.status_code == 400
    assert err.code == "edge_anchor_policy_violation"
    assert err.detail["resolution_policy"] == "none"
    assert "source_valid_from_version" in err.detail["offending_fields"]


# ---------------------------------------------------------------------------
# Extra: hypothetical post-merge version is suppressed by tombstone
#
# This is not in the tier-2 spec but confirms the strict-ancestor arm of
# the tombstone predicate fires. A test-only a9 document superseding a8
# would break CR-029's "chain head" precondition if seeded first, so we
# construct it after the merge (the merge closes at a8; a9 is a
# hypothetical extension of Chain A representing "queried in a world
# where someone still appended to the merged-away chain").
# ---------------------------------------------------------------------------


async def test_tombstone_suppresses_strict_downstream_version(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)
    await _seed_docs(graph_store, _id("c1"))
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    # After-the-fact a9 superseding a8 (e.g., operator repair scenario).
    await _seed_docs(graph_store, _id("a9"))
    await _seed_supersedes_chain(graph_store, [_id("a8"), _id("a9")])

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a9"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=3,
        )
    )
    # a9's lineage = {a9,a8,...,a1}. valid_until_version=a8 is a STRICT
    # ancestor of a9 -> suppress.
    assert out.nodes == []
