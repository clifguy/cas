"""Shared fixtures and builders for the out-of-band purge-tooling tests.

Most purge orchestration (dry-run, preconditions, typed confirmation, audit
ordering, halt-on-failure) is store-agnostic and runs against the in-memory
``StubGraphStore`` / ``StubContentStore``, which now implement ``remove_document``
and ``find_documents_ingested_between`` with real in-memory semantics. Only the
chain tests, which depend on real graph traversal the stub deliberately does not
fake, run against Postgres (the ``postgres_graph_store`` fixture in the parent
conftest).
"""

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, StagingEdge


def did(name: str) -> str:
    """A well-formed document id (8 hex + '_' + slug) derived from ``name``."""
    return hashlib.sha256(name.encode()).hexdigest()[:8] + "_" + name


@pytest.fixture
def stub_graph() -> StubGraphStore:
    return StubGraphStore()


@pytest.fixture
def stub_content() -> StubContentStore:
    return StubContentStore()


class StubAuditSink:
    """In-memory purge-audit sink recording appended records in order."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def append(self, record) -> None:
        self.records.append(dict(record))


@pytest.fixture
def stub_audit_sink() -> StubAuditSink:
    return StubAuditSink()


@pytest.fixture
def audit_records(stub_audit_sink):
    """Return a reader over the stub sink's appended records."""

    def _read() -> list[dict]:
        return list(stub_audit_sink.records)

    return _read


@pytest.fixture
def make_doc():
    def _make(
        name: str,
        *,
        created_at: datetime | None = None,
        pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
        tags: tuple[str, ...] = ("a", "b"),
    ) -> Document:
        now = created_at or datetime.now(timezone.utc)
        return Document(
            id=did(name),
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
            pipeline_status=pipeline_status,
            tags=list(tags),
            doc_type="ticket",
        )

    return _make


@pytest.fixture
def make_edge():
    def _make(src: str, tgt: str, edge_type: EdgeType = EdgeType.REFERENCES) -> Edge:
        return Edge(
            id=str(uuid.uuid4()),
            source_id=src,
            target_id=tgt,
            edge_type=edge_type,
            resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
            created_at=datetime.now(timezone.utc),
            rationale="r",
        )

    return _make


@pytest.fixture
def make_staging():
    def _make(src: str, tgt: str) -> StagingEdge:
        return StagingEdge(
            id=str(uuid.uuid4()),
            source_id=src,
            target_id=tgt,
            edge_type=EdgeType.REFERENCES,
            inference_evidence="e",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )

    return _make


@pytest.fixture
def make_chunk():
    def _make(document_id: str, index: int) -> Chunk:
        return Chunk(
            document_id=document_id,
            heading_path=f"h{index}",
            content="body",
            embedding=None,
            chunk_index=index,
            doc_type="ticket",
            lifecycle_status="active",
            project=None,
        )

    return _make
