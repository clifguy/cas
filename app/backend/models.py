"""CAS Application API Pydantic models.

Centralized response/request shapes for the /app/* HTTP surface,
mirroring ``components/schemas`` in
``docs/fs/cas_app_api.openapi.yaml`` with verbatim descriptions and
the typed aliases declared in the *CAS Typed-Alias Boundary
Conventions* steering document.

Coverage today: the scan chain (``ScanRequest``, ``ScanResponse``,
``ScanResultResponse``, ``ParsedMetadata``), the ingest request body
(``IngestRequest``, ``IngestFileItem``), and the shared
``ErrorResponse`` envelope (re-exported from ``sage.models.schemas``
so /scan and /ingest error responses match the YAML). SSE event
payloads remain to be ported in follow-up tickets.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sage.models.schemas import (
    DocumentDateStr,
    ErrorResponse,
    Sha256Str,
    VaultIdStr,
)

__all__ = [
    "ErrorResponse",
    "IngestFileItem",
    "IngestRequest",
    "ParsedMetadata",
    "ScanRequest",
    "ScanResponse",
    "ScanResultResponse",
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
    parsed_metadata: ParsedMetadata
    sage_status: Literal["new", "modified", "unchanged", "adapter_disabled", "no_adapter"] = Field(
        description="Vault-relative status for this file. See operation description."
    )


class ScanResponse(BaseModel):
    """Returned by /app/scan with per-file status and any scan-time warnings."""

    files: list[ScanResultResponse] = Field(
        description="Per-file scan results, one entry per file walked."
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
    parsed_metadata: ParsedMetadata | None = None


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
