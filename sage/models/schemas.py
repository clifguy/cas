"""Pydantic v2 models derived from the SAGE Core API OpenAPI specification.

lifecycle_status is str (not enum) because vaults define domain-specific
extensions like 'filed' that aren't in the base enum.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

from sage.models.enums import (
    CatalogSortBy,
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    ResponseLevel,
    RetrievalMode,
    RetrievalScope,
    SortOrder,
    SourceType,
    TraversalDirection,
    UserType,
)

# ---------------------------------------------------------------------------
# Shape-bearing primitive aliases.
#
# Pattern: each alias pairs a regex/parse helper with an ``Annotated``
# alias applied at every request-model field carrying that shape. The
# absence of an alias on a field with a known shape contract is the
# anomaly we want reviewers to notice.
# ---------------------------------------------------------------------------

# Document ID: 8 hex chars + "_" + slug. See sage/services/identity.py.
_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _validate_document_id(v: str) -> str:
    if not _DOCUMENT_ID_RE.match(v):
        raise ValueError(f"document id must match {_DOCUMENT_ID_RE.pattern!r} (got {v!r})")
    return v


DocumentIdStr = Annotated[str, AfterValidator(_validate_document_id)]


def _validate_edge_id(v: str) -> str:
    try:
        uuid.UUID(v)
    except ValueError as exc:
        raise ValueError(f"edge id must be a UUID (got {v!r})") from exc
    return v


EdgeIdStr = Annotated[str, AfterValidator(_validate_edge_id)]


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_sha256(v: str) -> str:
    if not _SHA256_RE.match(v):
        raise ValueError(f"hash must match {_SHA256_RE.pattern!r} (got {v!r})")
    return v


Sha256Str = Annotated[str, AfterValidator(_validate_sha256)]


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
    source_modified_at: datetime | None = None
    document_date: str | None = None
    semantic_abstract: str | None = None
    pipeline_status: PipelineStatus = PipelineStatus.PROJECTION_COMPLETE
    pipeline_error: str | None = None
    tier3_metadata: dict | None = None
    metadata_confirmed: bool = False


class DocumentSummary(BaseModel):
    id: str
    title: str
    lifecycle_status: str
    source_type: SourceType
    source_path: str | None = None
    version_label: str | None = None
    project: str | None = None
    doc_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    document_date: datetime | None = None
    source_modified_at: datetime | None = None
    semantic_abstract: str | None = None


class Edge(BaseModel):
    id: str
    source_id: str
    target_id: str | None = None
    edge_type: EdgeType
    resolution_policy: ResolutionPolicy | None = None
    source_valid_from_version: str | None = None
    target_valid_from_version: str | None = None
    valid_until_version: str | None = None
    retracted_edge_id: str | None = None
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
    needs_review: bool = False
    metadata: dict[str, str | list[str]] | None = None
    supersedes_document_id: DocumentIdStr | None = None

    @model_validator(mode="after")
    def _validate_metadata_dates(self) -> "IngestRequest":
        if self.metadata is None:
            return self
        for key in ("document_date", "date"):
            value = self.metadata.get(key)
            if isinstance(value, str):
                _validate_document_date(value)
        return self


class ParseFilenameRequest(BaseModel):
    filename: str
    adapter: SourceType


class ParseFilenameResponse(BaseModel):
    title: str | None = None
    project: str | None = None
    version_label: str | None = None
    document_date: str | None = None
    doc_type: str | None = None
    codes: list[str] | None = None


class DocumentWithContent(Document):
    """Document record optionally accompanied by file-delivery information.

    Field population depends on the `get_document` request mode:
    - Default request: all extra fields are None (base Document shape).
    - `include_content=true` (BH-117): `content` (base64 bytes) and
      `content_size` are populated.
    - `write_to_path=<path>` (BH-125): `written_to`, `content_size`, and
      `content_hash` are populated; `content` is None.
    """

    content: str | None = None
    content_size: int | None = None
    content_hash: str | None = None
    written_to: str | None = None


class SetLifecycleRequest(BaseModel):
    action: str
    new_version_id: DocumentIdStr | None = None


class SetLifecycleResponse(BaseModel):
    document: Document
    warnings: list[str] | None = None


def _coerce_tags(v: str | list[str] | None) -> list[str] | None:
    """Accept tags as a comma-separated string or a list of strings."""
    if v is None:
        return None
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    return v


_DOCUMENT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_document_date(v: str | None) -> str | None:
    """Reject values that are not the contract YYYY-MM-DD shape.

    The substrate stores ``document_date`` as a calendar-date string and
    every internal write path (filename parser, source_modified_at
    fallback) produces YYYY-MM-DD by construction. Caller-supplied
    values flow through verbatim, so a strict regex check at the
    boundary stops datetime-ISO strings (``2026-05-05T00:00:00Z``) from
    poisoning downstream readers that parse with ``strptime``.
    """
    if v is None:
        return v
    if not _DOCUMENT_DATE_RE.match(v):
        raise ValueError(f"document_date must be YYYY-MM-DD (got {v!r})")
    return v


DocumentDateStr = Annotated[str | None, AfterValidator(_validate_document_date)]


class UpdateMetadataRequest(BaseModel):
    title: str | None = None
    version_label: str | None = None
    project: str | None = None
    tags: Annotated[list[str] | None, BeforeValidator(_coerce_tags)] = None
    doc_type: str | None = None
    authority_scope: str | None = None
    document_date: DocumentDateStr = None


class RegisterUserRequest(BaseModel):
    display_name: str
    type: UserType


class IngestResponse(BaseModel):
    document: Document
    pipeline_status: PipelineStatus


class LinkRequest(BaseModel):
    source_id: DocumentIdStr
    target_id: DocumentIdStr | None = None
    edge_type: EdgeType
    source_valid_from_version: DocumentIdStr | None = None
    target_valid_from_version: DocumentIdStr | None = None
    retracted_edge_id: EdgeIdStr | None = None
    notes: str | None = None
    rationale: str | None = None


class ResolutionPathEntry(BaseModel):
    event_type: Literal["anchor_hit", "anchor_miss", "retracts_applied", "tombstone_applied"]
    edge_id: str
    anchor_field: Literal["source_valid_from_version", "target_valid_from_version"] | None = None
    anchor_version: str | None = None
    retracted_edge_id: str | None = None
    tombstone_version: str | None = None


class TraverseRequest(BaseModel):
    start_id: DocumentIdStr
    edge_type: EdgeType | None = None
    direction: TraversalDirection = TraversalDirection.OUTBOUND
    depth: int = Field(default=3, ge=1, le=1000)
    debug: bool = False


class TraversalNode(BaseModel):
    document: DocumentSummary
    edge: Edge
    depth: int
    edge_counts: dict[str, int] = Field(default_factory=lambda: {})


class TraverseResponse(BaseModel):
    start_id: str
    nodes: list[TraversalNode]
    resolution_path: list[ResolutionPathEntry] | None = None


class ChainRequest(BaseModel):
    document_id: DocumentIdStr
    edge_type: EdgeType
    limit: int | None = None
    offset: int = 0


class ChainEntry(BaseModel):
    id: str
    title: str
    version_label: str | None = None
    lifecycle_status: str
    document_date: str | None = None
    position: int


class ChainResponse(BaseModel):
    chain: list[ChainEntry]
    head_id: str
    tail_id: str
    query_position: int
    length: int
    total_length: int
    is_linear: bool
    available_edge_types: list[str] | None = None


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
    pipeline_status: str | None = None


class DiscoverRequest(BaseModel):
    mode: RetrievalMode = RetrievalMode.SEMANTIC
    query: str | None = None
    scope: RetrievalScope = RetrievalScope.ALL
    filters: RetrievalFilters | None = None
    document_id: str | None = None
    heading_path: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    use_hybrid: bool = True
    use_abstract_prefilter: bool = True
    include_abstracts: bool = False
    min_relevance: float | None = None
    response_level: ResponseLevel = ResponseLevel.CHUNKS
    sort_by: CatalogSortBy | None = None
    sort_order: SortOrder | None = None


class DiscoverHit(BaseModel):
    document: DocumentSummary
    chunk_content: str | None = None
    heading_path: str | None = None
    relevance_score: float | None = None
    matched_chunk_count: int | None = None


class DiscoverResponse(BaseModel):
    mode: RetrievalMode
    results: list[DiscoverHit]
    total_available: int
    hints: dict[str, object] | None = None


# ---------------------------------------------------------------------------
# Utility models
# ---------------------------------------------------------------------------


class ExportProjectionRequest(BaseModel):
    output_path: str


class ExportProjectionResponse(BaseModel):
    document_id: str
    output_path: str


class ReadProjectionResponse(BaseModel):
    document_id: str
    title: str
    version_label: str | None = None
    lifecycle_status: str
    doc_type: str | None = None
    source_path: str
    projection_text: str


class ReadSectionResponse(BaseModel):
    document_id: str
    title: str
    heading_path: str
    chunk_count: int
    section_text: str


class AssertionFailure(BaseModel):
    query: str
    expected_document_id: str
    top_k_checked: int
    found: bool
    actual_rank: int | None = None


class RefreshViewsResponse(BaseModel):
    vault_id: str
    views_generated: int


class EvalRetrievalResult(BaseModel):
    vault_id: str
    passed: bool
    assertion_count: int
    failure_count: int
    failures: list[AssertionFailure]


# ---------------------------------------------------------------------------
# Vault listing and statistics (BE-001 through BE-006)
# ---------------------------------------------------------------------------


class VaultDocTypeEntry(BaseModel):
    value: str
    label: str


class VaultLifecycleState(BaseModel):
    value: str
    label: str
    is_terminal: bool = False


class VaultAdapterInfo(BaseModel):
    source_type: str
    enabled: bool
    extensions: list[str]


class VaultSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    storage_root: str
    doc_types: list[VaultDocTypeEntry] = Field(default_factory=list)
    lifecycle_states: list[VaultLifecycleState] = Field(default_factory=list)
    adapters: list[VaultAdapterInfo] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)


class HealthIndicators(BaseModel):
    pending_metadata_count: int
    pending_edge_count: int
    deferred_abstract_count: int | None
    failed_ingestion_count: int


class VaultStatsResponse(BaseModel):
    total_documents: int
    by_lifecycle_state: dict[str, int]
    by_doc_type: dict[str, int]
    by_source_adapter: dict[str, int]
    total_edges: int
    by_edge_type: dict[str, int]
    staging_edge_count: int
    lancedb_size_bytes: int
    lancedb_chunk_count: int
    sqlite_size_bytes: int
    last_ingestion_at: datetime | None
    health: HealthIndicators


# ---------------------------------------------------------------------------
# Hash check (BE-007 through BE-009)
# ---------------------------------------------------------------------------


class HashCheckRequest(BaseModel):
    hashes: list[Sha256Str]


class HashCheckMatch(BaseModel):
    exists: bool
    document_id: str | None = None


# ---------------------------------------------------------------------------
# Vault config endpoints (PUT /sage_vaults/{vault_id}/config, POST /sage_vaults)
# ---------------------------------------------------------------------------


class UpdateVaultConfigRequest(BaseModel):
    """Section-level config update.  Only provided sections are replaced."""

    vault: dict | None = None
    document_types: dict | None = None
    lifecycle: dict | None = None
    source_adapters: dict | None = None
    metadata_extraction: dict | None = None
    edge_inference: dict | None = None
    abstraction: dict | None = None
    access_control_defaults: dict | None = None
    retrieval_health: dict | None = None


class CreateVaultRequest(BaseModel):
    """Full config dict for new vault creation."""

    config: dict


# ---------------------------------------------------------------------------
# Staging edges (BE-010 through BE-013)
# ---------------------------------------------------------------------------


class StagingEdge(BaseModel):
    id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    inference_evidence: str
    confidence_tier: int = 2
    created_at: datetime


# ---------------------------------------------------------------------------
# Pending metadata (BE-014 through BE-015)
# ---------------------------------------------------------------------------


class ExtractedField(BaseModel):
    value: str | None = None
    source: str  # "filename", "content", or "default"
    alt_value: str | None = None
    alt_source: str | None = None


class PendingMetadataItem(BaseModel):
    document: Document
    extracted_fields: dict[str, ExtractedField]


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict | None = None
