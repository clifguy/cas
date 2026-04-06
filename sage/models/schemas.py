"""Pydantic v2 models derived from the SAGE Core API OpenAPI specification.

lifecycle_status is str (not enum) because vaults define domain-specific
extensions like 'filed' that aren't in the base enum.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from sage.models.enums import EdgeType, PipelineStatus, SourceType, UserType


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


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict | None = None
