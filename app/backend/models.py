"""CAS Application API Pydantic models.

Centralized response/request shapes for the /app/* HTTP surface,
mirroring ``components/schemas`` in
``docs/fs/cas_app_api.openapi.yaml`` with verbatim descriptions and
the typed aliases declared in the *CAS Typed-Alias Boundary
Conventions* steering document.

Coverage today: the scan chain (``ScanRequest``, ``ScanResponse``,
``ScanResultResponse``, ``ParsedMetadata``) and the ingest request body
(``IngestRequest``, ``IngestFileItem``) are defined here. The SSE event
payloads (``ProgressEvent``, ``SummaryEvent``, ``DocumentsCreated``,
``EdgeWarning``, ``BatchIngestFileError``, and the ``IngestPreview`` /
``DocTypeRequirements`` pair a dry-run summary carries) and
the shared ``ErrorResponse`` envelope are re-exported from
``sage.models.schemas``: the bulk-ingest SSE shape is substrate-resident
so the co-located and hosted profiles emit identical events, and /scan
and /ingest error responses match the YAML.

The request models forbid undeclared fields (CAS-ADR-037). ``ParsedMetadata``
is closed in both directions on purpose: a scan returns it and a caller posts
it back into ingest unchanged, so one shape is what keeps that round trip
valid.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sage.models.schemas import (
    _ERROR_MODELS,
    AGENT_ARGUMENT_DESCRIPTION,
    AgentNameStr,
    BatchIngestFileError,
    DocTypeRequirements,
    DocumentDateStr,
    DocumentsCreated,
    EdgeWarning,
    ErrorResponse,
    IngestPreview,
    ProgressEvent,
    Sha256Str,
    SummaryEvent,
    VaultIdStr,
)

__all__ = [
    "BatchIngestFileError",
    "DocTypeRequirements",
    "DocumentsCreated",
    "EdgeWarning",
    "ErrorResponse",
    "IngestPreview",
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
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Tags for the document, in order, each carried whole: a tag that "
            "contains a comma stays one tag. `codes` sets the same field, so "
            "an entry supplying both is refused at "
            "`files.<n>.parsed_metadata.tags` before any file is ingested."
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
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="Absolute file path on disk.")
    source_type: str = Field(description="Source artifact format name to use for this file.")
    sha256: Sha256Str | None = Field(
        default=None,
        description=(
            "Optional SHA-256 of this file, as 64 hex characters in either case, bare or "
            "`sha256:`-prefixed. Checked before retention, including dry runs. A mismatch "
            "is a per-file `source_digest_mismatch`; a malformed string refuses the "
            "request with 400 `invalid_sha256` at `files.<index>.sha256`. Omission or "
            "null leaves the digest undeclared."
        ),
    )
    parsed_metadata: ParsedMetadata | None = Field(
        default=None,
        description=(
            "Optional caller-supplied parsed metadata for this file; when "
            "present, used as caller-authoritative input to ingest."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Optional Tier-3 metadata for this file, validated against the "
            "`metadata_schema` the vault config declares for the file's "
            "resolved doc_type (CAS-ADR-028). A payload the schema rejects, "
            "or any payload for a doc_type declaring no schema, is reported "
            "for that file as `tier3_schema_violation` and leaves the rest of "
            "the batch to run. A sibling of `parsed_metadata`, not a key in "
            "it: `parsed_metadata` carries the Tier-1 fields a filename "
            "parser can supply, and Tier-3 metadata never comes from a "
            "filename."
        ),
    )


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    needs_review: bool = Field(
        default=True,
        description=(
            "When true (default), every document in the batch lands with "
            "metadata_confirmed=false in the metadata-review queue "
            "(CAS-ADR-021). When false, caller-supplied metadata is committed "
            "as authoritative."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, evaluate every file and report one preview or one "
            "refusal per entry without persisting anything. No source is "
            "retained, no projection, indexing or abstraction runs, no "
            "record is written, and edge inference does not run whatever "
            "`infer_edges` says. Items are evaluated against committed "
            "state as it stood at batch start, so a sequential dependency "
            "within one batch is not reflected: two entries carrying "
            "identical bytes each report no duplicate."
        ),
    )
    agent: AgentNameStr | None = Field(
        default=None,
        description=AGENT_ARGUMENT_DESCRIPTION
        + " Applies to every document and production edge the batch writes.",
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


# Shared schema-derived error variants are part of both public specifications.

globals().update(_ERROR_MODELS)
