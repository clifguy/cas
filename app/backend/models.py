"""CAS Application API Pydantic models.

Centralized response/request shapes for the /app/* HTTP surface,
mirroring ``components/schemas`` in
``docs/fs/cas_app_api.openapi.yaml`` with verbatim descriptions and
the typed aliases declared in the *CAS Typed-Alias Boundary
Conventions* steering document.

Coverage today: the scan chain (``ScanRequest``, ``ScanResponse``,
``ScanResultResponse``, ``ParsedMetadata``), the ingest request body
(``IngestRequest``, ``IngestFileItem``), the SSE event payloads
(``ProgressEvent``, ``SummaryEvent``, ``DocumentsCreated``), and the
shared ``ErrorResponse`` envelope (re-exported from
``sage.models.schemas`` so /scan and /ingest error responses match
the YAML).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sage.models.schemas import (
    DocumentDateStr,
    DocumentIdStr,
    ErrorResponse,
    Sha256Str,
    VaultIdStr,
)

__all__ = [
    "DocumentsCreated",
    "ErrorResponse",
    "IngestFileItem",
    "IngestRequest",
    "ParsedMetadata",
    "ProgressEvent",
    "ScanRequest",
    "ScanResponse",
    "ScanResultResponse",
    "SummaryEvent",
]


class ScanRequest(BaseModel):
    vault_id: VaultIdStr = Field(description="Target vault identifier.")
    directory: str = Field(
        description=(
            "Absolute or user-relative directory path. Surrounding single "
            "or double quotes are stripped to tolerate paste-with-quotes "
            "from terminals or finder."
        )
    )
    max_depth: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional recursion ceiling. 0 = scan the directory root only, "
            "1 = root plus one subdirectory level, etc. Null = unlimited "
            "depth."
        ),
    )


class ParsedMetadata(BaseModel):
    title: str = Field(description="Human-readable title extracted from the filename or content.")
    date: DocumentDateStr = Field(
        default=None,
        description="Document calendar date (YYYY-MM-DD) extracted from filename.",
    )
    project: str | None = Field(
        default=None,
        description=(
            "Project identifier extracted from filename when the vault's "
            "project_identifier rule matches a segment."
        ),
    )
    codes: list[str] = Field(
        default_factory=list,
        description=(
            "Code identifiers extracted from filename via known_code_patterns "
            '(e.g. "PV07", "CD-2"). Order preserved from the filename.'
        ),
    )
    version: str | None = Field(
        default=None,
        description='Canonical version label (e.g. "v1.2.0", "v3.0").',
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Document type resolved by the vault's keyword_to_doc_type and "
            "code_to_doc_type rules. Null if no rule matches."
        ),
    )


class ScanResultResponse(BaseModel):
    file_path: str = Field(description="Absolute file path on disk.")
    file_hash: Sha256Str = Field(description="SHA-256 hex digest of file contents.")
    source_modified_at: str = Field(description="File mtime (st_mtime) as an ISO 8601 timestamp.")
    adapter: str | None = Field(
        default=None,
        description=(
            "Source adapter name matching this extension, or null when no "
            "registered adapter handles the extension."
        ),
    )
    parsed_metadata: ParsedMetadata = Field(
        description="Filename-derived metadata extracted for this file.",
    )
    sage_status: Literal["new", "modified", "unchanged", "adapter_disabled", "no_adapter"] = Field(
        description="Vault-relative status for this file. See operation description."
    )


class ScanResponse(BaseModel):
    """Returned by /app/scan with per-file status and any scan-time warnings."""

    files: list[ScanResultResponse] = Field(
        description="Per-file scan results in directory-walk order."
    )
    warnings: list[str] = Field(
        description=(
            "Non-fatal scan warnings (e.g. unreadable file, malformed "
            "symlink). Files that produced warnings may also appear in "
            'files[] with status "no_adapter" or be omitted depending on '
            "the warning class."
        )
    )


class IngestFileItem(BaseModel):
    file_path: str = Field(description="Absolute file path on disk.")
    adapter: str = Field(description="Source adapter name to use for this file.")
    parsed_metadata: ParsedMetadata | None = Field(
        default=None,
        description=(
            "Optional caller-supplied parsed metadata for this file; when "
            "present, used as caller-authoritative input to ingest."
        ),
    )


class IngestRequest(BaseModel):
    vault_id: VaultIdStr = Field(description="Target vault identifier.")
    files: list[IngestFileItem] = Field(description="Files to ingest. Empty list returns 400.")
    infer_edges: bool = Field(
        default=True,
        description=(
            "When true, the post-ingest phase runs version_chain (Tier 1 "
            "supersedes) and filename_code_match (Tier 2 covers) edge "
            "inference. When false, edges are not inferred; ingestion is "
            "otherwise unchanged."
        ),
    )


class DocumentsCreated(BaseModel):
    """Nested counter for ``SummaryEvent.documents_created``."""

    new: int = Field(description="Documents newly inserted this batch.")
    new_version: int = Field(
        description="Documents inserted as a new version of an existing chain.",
    )


class ProgressEvent(BaseModel):
    """SSE ``progress`` event payload.

    Emitted on file start, completion, and error during the per-file
    ingestion phase.
    """

    event_type: Literal["progress"] = Field(
        description="Discriminator for the SSE event payload variant; always 'progress'.",
    )
    file_index: int = Field(description="Zero-based index of this file in the batch.")
    total_files: int = Field(description="Total file count in the batch.")
    filename: str = Field(description="Filename this progress event refers to.")
    stage: Literal["projection"] = Field(
        description="Pipeline stage the event applies to.",
    )
    status: Literal["started", "completed", "failed"] = Field(
        description="Per-file processing status the event reports.",
    )
    document_id: DocumentIdStr | None = Field(
        default=None,
        description='Set when status="completed". Document ID assigned by SAGE.',
    )
    error: str | None = Field(
        default=None,
        description='Set when status="failed". Caller-facing error message.',
    )


class SummaryEvent(BaseModel):
    """SSE ``summary`` event payload.

    Emitted once at the end of the batch, after all per-file events.
    Mirrors the ``IngestSummary`` dataclass returned to non-streaming
    callers (MCP tool).
    """

    event_type: Literal["summary"] = Field(
        description="Discriminator for the SSE event payload variant; always 'summary'.",
    )
    documents_created: DocumentsCreated = Field(
        description=(
            "Document-creation counts for this batch, split by whether the "
            "document opens a chain or extends one."
        ),
    )
    metadata_pending: int = Field(
        description=(
            "Count of documents in this batch whose extracted metadata is not yet confirmed."
        ),
    )
    edges_created: dict[str, int] = Field(
        description="Tier 1 edges created in production, keyed by edge type.",
    )
    edges_staged: dict[str, int] = Field(
        description="Tier 2 edges inserted into staging_edges, keyed by edge type.",
    )
    edges_removed: int = Field(
        description="Pre-existing edges removed during this batch (e.g. supersession cleanup).",
    )
    edges_dropped: int = Field(
        description=(
            "Edges in the original plan that were dropped because their "
            "referenced files failed to ingest."
        ),
    )
    abstracts_generated: int = Field(
        description=(
            "Number of documents for which a semantic abstract was generated during this batch."
        ),
    )
    abstracts_deferred: int = Field(
        description=(
            "Documents for which abstraction was skipped (empty projection "
            "or abstraction disabled in vault config)."
        ),
    )
    error_count: int = Field(
        description="Total number of per-file errors recorded during the batch.",
    )
    errors: list[dict[str, Any]] = Field(
        description="Per-file error records for files that failed to ingest in this batch.",
    )
    edge_warnings: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Optional edge-inference warnings (e.g. ambiguous version "
            "chains). Present only when warnings were produced."
        ),
    )
