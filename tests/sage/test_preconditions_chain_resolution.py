"""check_preconditions resolves depends_on through version chains (CAS-ADR-017).

Edge collection follows the same chain-scoped resolution as ``traverse``
(lineage seeding, anchor filter, ``retracts``, ``merged_from`` tombstones),
and each visible edge's target is judged on its supersedes-chain head.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, LinkRequest


def _id(name: str) -> str:
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(
    name: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    now = datetime.now(timezone.utc)
    doc_id = _id(name)
    return Document(
        id=doc_id,
        title=f"Title {name}",
        doc_type="plan",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{name}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(name),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
    )


async def _supersede(graph_store, newer: str, older: str, offset: int = 1) -> None:
    await graph_store.insert_edge(
        Edge(
            id=str(uuid.uuid4()),
            source_id=_id(newer),
            target_id=_id(older),
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=offset),
        )
    )


async def _depend(graph_ops_service, source: str, target: str) -> Edge:
    return (
        await graph_ops_service._create_edge_strict(
            LinkRequest(
                source_id=_id(source),
                target_id=_id(target),
                edge_type=EdgeType.DEPENDS_ON,
                source_valid_from_version=_id(source),
                target_valid_from_version=_id(target),
            )
        )
    ).edge


async def test_superseded_target_is_judged_on_its_chain_head(graph_store, graph_ops_service):
    """The edge names an archived predecessor; the chain's active head satisfies it."""
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("t0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("t1", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("t2"))
    await _supersede(graph_store, "t1", "t0", 1)
    await _supersede(graph_store, "t2", "t1", 2)
    await _depend(graph_ops_service, "fn", "t0")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.target_id == _id("t0")
    assert check.head_id == _id("t2")
    assert check.title == "Title t2"
    assert check.actual == "active"
    assert check.satisfied is True
    assert result.satisfied is True


async def test_head_pipeline_failure_overrides_a_satisfying_stored_target(
    graph_store, graph_ops_service
):
    """Satisfaction is the head's: a completed predecessor does not stand in for it."""
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("t0", lifecycle_status="completed"))
    await graph_store.insert_document(_make_doc("t1", pipeline_status=PipelineStatus.FAILED))
    await _supersede(graph_store, "t1", "t0")
    await _depend(graph_ops_service, "fn", "t0")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.head_id == _id("t1")
    assert check.actual == "failed (pipeline_incomplete)"
    assert check.satisfied is False


async def test_superseded_source_inherits_the_predecessor_edge(graph_store, graph_ops_service):
    """The new head of a superseded source sees its predecessor's dependency.

    Before chain resolution the head had no outbound edges of its own and
    the check passed vacuously with ``checks: []``.
    """
    await graph_store.insert_document(_make_doc("s0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("s1"))
    await graph_store.insert_document(_make_doc("blocker", lifecycle_status="archived"))
    await _supersede(graph_store, "s1", "s0")
    await _depend(graph_ops_service, "s0", "blocker")

    result = await graph_ops_service.check_preconditions(_id("s1"))

    (check,) = result.checks
    assert check.target_id == _id("blocker")
    assert check.head_id == _id("blocker")
    assert check.actual == "archived"
    assert check.satisfied is False
    assert result.satisfied is False


async def test_inherited_and_own_edge_to_one_target_report_one_row(graph_store, graph_ops_service):
    """A target reached from two versions of the source is checked once."""
    await graph_store.insert_document(_make_doc("s0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("s1"))
    await graph_store.insert_document(_make_doc("dep"))
    await graph_store.insert_document(_make_doc("other"))
    await _supersede(graph_store, "s1", "s0")
    await _depend(graph_ops_service, "s0", "dep")
    await _depend(graph_ops_service, "s0", "other")
    await _depend(graph_ops_service, "s1", "dep")

    result = await graph_ops_service.check_preconditions(_id("s1"))

    assert sorted(c.target_id for c in result.checks) == sorted([_id("dep"), _id("other")])


async def test_edge_anchored_after_the_queried_version_is_not_visible(
    graph_store, graph_ops_service
):
    """An edge added at a later version does not reach back to an earlier one."""
    await graph_store.insert_document(_make_doc("s0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("s1"))
    await graph_store.insert_document(_make_doc("blocker", lifecycle_status="archived"))
    await _supersede(graph_store, "s1", "s0")
    await _depend(graph_ops_service, "s1", "blocker")

    earlier = await graph_ops_service.check_preconditions(_id("s0"))
    later = await graph_ops_service.check_preconditions(_id("s1"))

    assert earlier.checks == []
    assert earlier.satisfied is True
    assert [c.target_id for c in later.checks] == [_id("blocker")]
    assert later.satisfied is False


async def test_retracted_edge_is_not_counted(graph_store, graph_ops_service):
    """A retraction anchored in the queried lineage suppresses the dependency."""
    await graph_store.insert_document(_make_doc("a0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("a1"))
    await graph_store.insert_document(_make_doc("blocker", lifecycle_status="archived"))
    await _supersede(graph_store, "a1", "a0")
    edge = await _depend(graph_ops_service, "a0", "blocker")
    await graph_ops_service._create_edge_strict(
        LinkRequest(
            source_id=_id("a1"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id=edge.id,
            source_valid_from_version=_id("a1"),
        )
    )

    retracted = await graph_ops_service.check_preconditions(_id("a1"))
    before_retraction = await graph_ops_service.check_preconditions(_id("a0"))

    assert retracted.checks == []
    assert retracted.satisfied is True
    assert [c.target_id for c in before_retraction.checks] == [_id("blocker")]
    assert before_retraction.satisfied is False


async def test_forked_target_chain_is_reported_not_resolved(graph_store, graph_ops_service):
    """Two heads is a fork: the row names it and picks neither."""
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("t0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("t1a"))
    await graph_store.insert_document(_make_doc("t1b"))
    await _supersede(graph_store, "t1a", "t0", 1)
    await _supersede(graph_store, "t1b", "t0", 2)
    await _depend(graph_ops_service, "fn", "t0")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.target_id == _id("t0")
    assert check.head_id is None
    assert check.title is None
    assert check.doc_type is None
    assert check.actual == "forked (2 heads)"
    assert check.satisfied is False
    assert result.satisfied is False


async def test_cyclic_target_chain_is_reported_not_raised(graph_store, graph_ops_service):
    """A supersedes cycle has no head: the row says so instead of erroring."""
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("pa"))
    await graph_store.insert_document(_make_doc("pb"))
    await _supersede(graph_store, "pb", "pa", 1)
    await _supersede(graph_store, "pa", "pb", 2)
    await _depend(graph_ops_service, "fn", "pa")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.target_id == _id("pa")
    assert check.head_id is None
    assert check.title is None
    assert check.actual == "cyclic (no head)"
    assert check.satisfied is False
    assert result.satisfied is False


async def test_fork_beside_the_target_does_not_count_as_its_fork(graph_store, graph_ops_service):
    """Only the target's own successors decide its head, not a sibling branch."""
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("root", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("t0", lifecycle_status="archived"))
    await graph_store.insert_document(_make_doc("sibling"))
    await graph_store.insert_document(_make_doc("t1"))
    await _supersede(graph_store, "t0", "root", 1)
    await _supersede(graph_store, "sibling", "root", 2)
    await _supersede(graph_store, "t1", "t0", 3)
    await _depend(graph_ops_service, "fn", "t0")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.head_id == _id("t1")
    assert check.satisfied is True


async def test_unsuperseded_target_is_its_own_head(graph_store, graph_ops_service):
    await graph_store.insert_document(_make_doc("fn"))
    await graph_store.insert_document(_make_doc("dep"))
    await _depend(graph_ops_service, "fn", "dep")

    result = await graph_ops_service.check_preconditions(_id("fn"))

    (check,) = result.checks
    assert check.target_id == _id("dep")
    assert check.head_id == _id("dep")
    assert check.title == "Title dep"
    assert check.satisfied is True
