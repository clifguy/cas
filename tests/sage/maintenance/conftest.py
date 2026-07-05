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
import json
import uuid
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.models.enums import EdgeType, PipelineStatus, ResolutionPolicy, SourceType
from sage.models.schemas import Document, Edge, StagingEdge
from sage.services.maintenance_log import MAINTENANCE_LOG_FILENAME


def did(name: str) -> str:
    """A well-formed document id (8 hex + '_' + slug) derived from ``name``."""
    return hashlib.sha256(name.encode()).hexdigest()[:8] + "_" + name


@pytest.fixture
def stub_graph() -> StubGraphStore:
    return StubGraphStore()


@pytest.fixture
def stub_content() -> StubContentStore:
    return StubContentStore()


@pytest.fixture
def vault_dir(tmp_path):
    """Directory that stands in for the vault dir where the audit log is written."""
    return tmp_path


@pytest.fixture
def audit_records(vault_dir):
    """Return a reader that parses the vault's ``.maintenance_log.jsonl`` lines."""

    def _read() -> list[dict]:
        log_path = vault_dir / MAINTENANCE_LOG_FILENAME
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]

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
