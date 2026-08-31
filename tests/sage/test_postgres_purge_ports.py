"""GraphStore port support for out-of-band operator purge tooling.

Two methods exist solely to let the ``sage.maintenance`` purge CLI remove state
through the port rather than reaching into Postgres internals:

- ``remove_document`` -- delete a document's whole graph footprint (row, tags,
  edges, staging edges) in one transaction. The edge/staging FKs to
  ``documents`` have no ``ON DELETE CASCADE``, so the delete order is
  load-bearing: edges before the document row, or the delete is FK-blocked.
- ``find_documents_ingested_between`` -- select documents by a half-open
  ``created_at`` window, the batch-purge selector.

Both run against a real Postgres via the ``postgres_graph_store`` fixture, which
skips without ``SAGE_TEST_PG_DSN``. Cross-store coordination (also removing the
content chunks) is not the port's job (CAS-ADR-042 weakest-binding) and is
covered in the maintenance-package tests.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, StagingEdge

pytest.importorskip("sage.storage.postgres.graph_store")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _did(name: str) -> str:
    """A well-formed document id (8 hex + '_' + slug) derived from ``name``."""
    return hashlib.sha256(name.encode()).hexdigest()[:8] + "_" + name


def _doc(
    name: str, *, created_at: datetime | None = None, tags: list[str] | None = None
) -> Document:
    now = created_at or datetime.now(timezone.utc)
    return Document(
        id=_did(name),
        title=f"Doc {name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"/x/{name}.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + hashlib.sha256(name.encode()).hexdigest(),
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        tags=tags if tags is not None else ["a", "b"],
        doc_type="ticket",
    )


def _edge(src: str, tgt: str, edge_type: EdgeType = EdgeType.REFERENCES) -> Edge:
    return Edge(
        id=str(uuid.uuid4()),
        source_id=src,
        target_id=tgt,
        edge_type=edge_type,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        created_at=datetime.now(timezone.utc),
        rationale="r",
    )


def _staging(src: str, tgt: str) -> StagingEdge:
    return StagingEdge(
        id=str(uuid.uuid4()),
        source_id=src,
        target_id=tgt,
        edge_type=EdgeType.REFERENCES,
        inference_evidence="e",
        confidence_tier=2,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# remove_document
# ---------------------------------------------------------------------------


async def test_remove_document_deletes_full_graph_footprint(postgres_graph_store):
    """The whole footprint of the target -- its row, tags, both edge
    directions, and staging edges -- is gone; an unrelated control document and
    its edge/tags survive. If the impl deleted the documents row before the
    edges, the un-cascaded FK would raise and this test would error."""
    store = postgres_graph_store
    for d in (_doc("target"), _doc("n1"), _doc("n2"), _doc("control")):
        await store.insert_document(d)
    await store.insert_edge(_edge(_did("target"), _did("n1")))  # outbound from target
    await store.insert_edge(_edge(_did("n2"), _did("target")))  # inbound to target
    await store.insert_edge(_edge(_did("control"), _did("n1")))  # control's edge
    await store.insert_staging_edge(_staging(_did("target"), _did("n1")))

    await store.remove_document(_did("target"))

    assert await store.get_document(_did("target")) is None
    assert await store.get_edges_by_source(_did("target")) == []
    assert await store.get_edges_by_target(_did("target")) == []
    staging_touching_target = [
        s for s in await store.list_staging_edges() if _did("target") in (s.source_id, s.target_id)
    ]
    assert staging_touching_target == []

    control = await store.get_document(_did("control"))
    assert control is not None
    assert control.tags == ["a", "b"]
    control_edges = await store.get_edges_by_source(_did("control"))
    assert [e.target_id for e in control_edges] == [_did("n1")]


async def test_remove_document_missing_is_noop(postgres_graph_store):
    """Removing an id with no document row is a silent no-op: no error, and an
    unrelated document is untouched."""
    store = postgres_graph_store
    await store.insert_document(_doc("keep"))

    await store.remove_document(_did("does_not_exist"))

    assert await store.get_document(_did("keep")) is not None


# ---------------------------------------------------------------------------
# find_documents_ingested_between
# ---------------------------------------------------------------------------


async def test_find_documents_ingested_between_is_half_open(postgres_graph_store):
    """The window is [since, until): the doc exactly at ``since`` is included,
    the doc exactly at ``until`` is excluded, and results come back ordered by
    ``created_at``. Flipping the upper bound to ``<=`` would wrongly include the
    ``until``-edge doc."""
    store = postgres_graph_store
    since = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    until = since + timedelta(minutes=5)
    placements = {
        "before": since - timedelta(minutes=1),
        "at_since": since,
        "mid": since + timedelta(minutes=1),
        "near_until": until - timedelta(minutes=1),
        "at_until": until,
        "after": until + timedelta(minutes=1),
    }
    for name, ts in placements.items():
        await store.insert_document(_doc(name, created_at=ts))

    result = await store.find_documents_ingested_between(since, until)

    assert [d.id for d in result] == [_did("at_since"), _did("mid"), _did("near_until")]


async def test_find_documents_ingested_between_open_top(postgres_graph_store):
    """``until=None`` leaves the window open at the top -- a far-future doc is
    returned; an explicit near upper bound excludes it. Proves the upper bound
    is actually applied rather than ignored."""
    store = postgres_graph_store
    since = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    await store.insert_document(_doc("at_since", created_at=since))
    far_future = since + timedelta(days=365)
    await store.insert_document(_doc("far", created_at=far_future))

    open_top = await store.find_documents_ingested_between(since, None)
    assert {d.id for d in open_top} == {_did("at_since"), _did("far")}

    bounded = await store.find_documents_ingested_between(since, since + timedelta(minutes=5))
    assert {d.id for d in bounded} == {_did("at_since")}
