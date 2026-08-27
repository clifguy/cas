"""CAS Application API Pydantic models.

Centralized response/request shapes for the /app/* HTTP surface,
mirroring ``components/schemas`` in
``docs/fs/cas_app_api.openapi.yaml`` with verbatim descriptions and
the typed aliases declared in the *CAS Typed-Alias Boundary
Conventions* steering document.

Coverage today: the scan chain (``ScanRequest``, ``ScanResponse``,
``ScanResultResponse``, ``ParsedMetadata``) and the ingest request body
(``IngestRequest``, ``IngestFileItem``) are defined here. The SSE event
payloads (``ProgressEvent``, ``SummaryEvent``, ``DocumentsCreated``) and
the shared ``ErrorResponse`` envelope are re-exported from
``sage.models.schemas``: the bulk-ingest SSE shape is substrate-resident
so the co-located and hosted profiles emit identical events, and /scan
and /ingest error responses match the YAML.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sage.models.schemas import (
    DocumentDateStr,
    DocumentsCreated,
    ErrorResponse,
    ProgressEvent,
    Sha256Str,
    SummaryEvent,
    VaultIdStr,
)

__all__ = [
    "DocumentsCreated",
    "ErrorResponse",
    "IngestFileItem",
    "IngestRequest",
    "LoginChallengeResponse",
    "ParsedMetadata",
    "ProgressEvent",
    "ScanRequest",
    "ScanResponse",
    "ScanResultResponse",
    "SessionInfoResponse",
    "SummaryEvent",
    "UserClaims",
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
            "1 = root plus one subdirectory level, etc. Null = the server's "
            "default depth ceiling; a scan cut by that default is reported "
            "as truncated."
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
    source_type: str | None = Field(
        default=None,
        description=(
            "Source artifact format matching this extension, or null "
            "when no registered adapter handles the extension."
        ),
    )
    parsed_metadata: ParsedMetadata = Field(
        description="Filename-derived metadata extracted for this file.",
    )
    sage_status: Literal["new", "modified", "unchanged", "no_adapter"] = Field(
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
    truncated: bool = Field(
        default=False,
        description=(
            "True when a scan ceiling (file count, hashed bytes, or the "
            "default depth) cut the walk short; the warnings list names "
            "the ceiling that fired."
        ),
    )


class IngestFileItem(BaseModel):
    file_path: str = Field(description="Absolute file path on disk.")
    source_type: str = Field(description="Source artifact format name to use for this file.")
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


class LoginChallengeResponse(BaseModel):
    """Returned by /app/auth/login to start the interactive sign-in."""

    authorization_url: str = Field(
        description=(
            "Identity-provider authorization URL the browser is sent to in "
            "order to sign in. The single-page app navigates the browser to "
            "this URL."
        )
    )
    state: str = Field(
        description=(
            "Opaque anti-forgery value bound to this sign-in attempt; echoed "
            "back on the callback and validated server-side."
        )
    )


class UserClaims(BaseModel):
    """Identity-provider claims describing the signed-in user."""

    subject: str = Field(
        description=(
            "Stable identity-provider subject for the signed-in user (the "
            "directory object id, or the OIDC subject when that is absent)."
        )
    )
    name: str | None = Field(
        default=None,
        description=(
            "Human-readable display name from the identity-provider claims, when present."
        ),
    )
    email: str | None = Field(
        default=None,
        description=(
            "Sign-in email or user-principal name from the identity-provider claims, when present."
        ),
    )


class SessionInfoResponse(BaseModel):
    """Returned by /app/auth/me describing the caller's session state."""

    authenticated: bool = Field(
        description=("True when the request carries a live server-side session; false otherwise.")
    )
    user: UserClaims | None = Field(
        default=None,
        description=(
            "The signed-in user's identity claims when authenticated; null "
            "when no live session is present."
        ),
    )
