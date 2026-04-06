"""Pydantic v2 models derived from the SAGE Core API OpenAPI specification.

lifecycle_status is str (not enum) because vaults define domain-specific
extensions like 'filed' that aren't in the base enum.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RetrievalMode,
    RetrievalScope,
    SourceType,
    TraversalDirection,
    UserType,
)


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

class Document(BaseModel):
    id: str
    title: str
    source_type: SourceType
    source_path: str
    lifecycle_status: str = "active"
    version_label: str | None = None
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    authority_scope: str | None = None
    doc_type: str | None = None
    source_content_hash: str
    adapter_version: str
    created_by: str
    created_at: datetime
    last_modified_by: str
    updated_at: datetime
    projected_at: datetime | None = None
    indexed_at: datetime | None = None
    semantic_abstract: str | None = None
    pipeline_status: PipelineStatus = PipelineStatus.PROJECTION_COMPLETE
    pipeline_error: str | None = None
    tier3_metadata: dict | None = None


class DocumentSummary(BaseModel):
    id: str
    title: str
    lifecycle_status: str
    source_type: SourceType
    version_label: str | None = None
    project: str | None = None
    doc_type: str | None = None
    tags: list[str] = Field(default_factory=list)


class Edge(BaseModel):
    id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    created_at: datetime
    notes: str | None = None
    rationale: str | None = None


class User(BaseModel):
    id: str
    display_name: str
    type: UserType
    created_at: datetime


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    source: str
    adapter: SourceType
    config: dict | None = None
    created_by: str | None = None
    force: bool = False


class SetLifecycleRequest(BaseModel):
    action: str
    new_version_id: str | None = None


class SetLifecycleResponse(BaseModel):
    document: Document
    warnings: list[str] | None = None


class UpdateMetadataRequest(BaseModel):
    title: str | None = None
    version_label: str | None = None
    project: str | None = None
    tags: list[str] | None = None
    doc_type: str | None = None
    authority_scope: str | None = None


class RegisterUserRequest(BaseModel):
    display_name: str
    type: UserType


class IngestResponse(BaseModel):
    document: Document
    pipeline_status: PipelineStatus


class LinkRequest(BaseModel):
    source_id: str
    target_id: str
    edge_type: EdgeType
    notes: str | None = None
    rationale: str | None = None


class TraverseRequest(BaseModel):
    start_id: str
    edge_type: EdgeType | None = None
    direction: TraversalDirection = TraversalDirection.OUTBOUND
    depth: int = Field(default=3, ge=1, le=50)


class TraversalNode(BaseModel):
    document: DocumentSummary
    edge: Edge
    depth: int
    edge_count: int = 1


class TraverseResponse(BaseModel):
    start_id: str
    nodes: list[TraversalNode]


class PreconditionCheck(BaseModel):
    target_id: str
    required: str
    actual: str
    satisfied: bool


class PreconditionResult(BaseModel):
    function_id: str
    satisfied: bool
    checks: list[PreconditionCheck]


# ---------------------------------------------------------------------------
# Discover (retrieval) models
# ---------------------------------------------------------------------------

class RetrievalFilters(BaseModel):
    doc_type: str | None = None
    project: str | None = None
    lifecycle_status: str | None = None
    tags: list[str] | None = None
    document_ids: list[str] | None = None


class DiscoverRequest(BaseModel):
    mode: RetrievalMode
    query: str | None = None
    scope: RetrievalScope = RetrievalScope.ALL
    filters: RetrievalFilters | None = None
    document_id: str | None = None
    heading_path: str | None = None
    authority_document_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
    cursor: str | None = None
    use_hybrid: bool = False


class DiscoverHit(BaseModel):
    document: DocumentSummary
    chunk_content: str | None = None
    heading_path: str | None = None
    relevance_score: float | None = None


class DiscoverResponse(BaseModel):
    mode: RetrievalMode
    results: list[DiscoverHit]
    total_available: int
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Utility models
# ---------------------------------------------------------------------------

class ExportProjectionRequest(BaseModel):
    output_path: str


class ExportProjectionResponse(BaseModel):
    document_id: str
    output_path: str


class AssertionFailure(BaseModel):
    query: str
    expected_document_id: str
    top_k_checked: int
    found: bool
    actual_rank: int | None = None


class EvalRetrievalResult(BaseModel):
    vault_id: str
    passed: bool
    assertion_count: int
    failure_count: int
    failures: list[AssertionFailure]


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict | None = None
