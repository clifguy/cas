"""Pydantic v2 models derived from the SAGE Core API OpenAPI specification.

lifecycle_status is str (not enum) because vaults define domain-specific
extensions like 'filed' that aren't in the base enum.
"""

import re
import uuid
from datetime import date, datetime
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
    if not _DOCUMENT_ID_RE.fullmatch(v):
        raise ValueError(f"document id must match {_DOCUMENT_ID_RE.pattern!r} (got {v!r})")
    return v


DocumentIdStr = Annotated[str, AfterValidator(_validate_document_id)]


def _validate_edge_id(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except ValueError as exc:
        raise ValueError(f"edge id must be a UUID (got {v!r})") from exc


EdgeIdStr = Annotated[str, AfterValidator(_validate_edge_id)]


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_sha256(v: str) -> str:
    if not _SHA256_RE.fullmatch(v):
        raise ValueError(f"hash must match {_SHA256_RE.pattern!r} (got {v!r})")
    return v


Sha256Str = Annotated[str, AfterValidator(_validate_sha256)]


_DOCUMENT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_document_date(v: str | None) -> str | None:
    """Reject values that are not a YYYY-MM-DD calendar date.

    The substrate stores ``document_date`` as a calendar-date string and
    every internal write path (filename parser, source_modified_at
    fallback) produces YYYY-MM-DD by construction. Caller-supplied
    values flow through verbatim, so a strict regex check at the
    boundary stops datetime-ISO strings (``2026-05-05T00:00:00Z``) from
    poisoning downstream readers that parse with ``strptime``. The
    follow-up calendar-validity check (``date.fromisoformat``) rejects
    shape-valid-but-impossible strings like ``2026-02-30``.
    """
    if v is None:
        return v
    if not _DOCUMENT_DATE_RE.fullmatch(v):
        raise ValueError(f"document_date must be YYYY-MM-DD (got {v!r})")
    try:
        date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"document_date must be a valid calendar date (got {v!r})") from exc
    return v


DocumentDateStr = Annotated[str | None, AfterValidator(_validate_document_date)]


def _validate_user_id(v: str) -> str:
    """Normalize an RFC 4122 UUID to canonical lowercase hyphenated form.

    User IDs are generated as ``str(uuid.uuid4())`` by the
    user-registration service (sage/services/user_service.py). The
    canonical form is 8-4-4-4-12 hex digits separated by hyphens,
    lowercase. ``uuid.UUID()`` accepts urn-prefixed (``urn:uuid:...``),
    brace-wrapped (``{...}``), hex-no-hyphens (32 hex chars), and
    mixed-case variants; this validator accepts them and normalizes to
    canonical so the downstream substrate keys on a single form.
    Flavor: normalize.
    """
    try:
        return str(uuid.UUID(v))
    except ValueError as exc:
        raise ValueError(f"user id must be a UUID (got {v!r})") from exc


UserIdStr = Annotated[str, AfterValidator(_validate_user_id)]


_VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_vault_id(v: str) -> str:
    """Reject vault ids that do not match the slug shape contract.

    A ``vault_id`` is used as a filesystem path segment under
    ``~/sage_vaults/{vault_id}/`` and as a primary key in the loaded
    vault registry. The shape is a lowercase slug: ASCII alphanumeric
    plus underscore and hyphen, starting with alphanumeric, max 64
    chars. The producing source is the directory name on disk; this
    validator stops path-traversal and mixed-case caller input from
    propagating to filesystem and registry lookups downstream.
    Flavor: reject.
    """
    if not _VAULT_ID_RE.fullmatch(v):
        raise ValueError(f"vault id must match {_VAULT_ID_RE.pattern!r} (got {v!r})")
    return v


VaultIdStr = Annotated[str, AfterValidator(_validate_vault_id)]


def _validate_function_id(v: str) -> str:
    """Reject function ids that do not match the document-id shape.

    A ``function_id`` references a function-document in the precondition
    system; ``GraphOps.check_preconditions`` looks it up via
    ``get_document(function_id)``. The shape is therefore identical to
    DocumentIdStr (8 hex chars + ``_`` + slug). Distinct alias to
    communicate that the field carries function-document semantics, not
    arbitrary document semantics — a future divergence would land here
    without disturbing DocumentIdStr's call sites.
    Flavor: reject.
    """
    if not _DOCUMENT_ID_RE.fullmatch(v):
        raise ValueError(f"function id must match {_DOCUMENT_ID_RE.pattern!r} (got {v!r})")
    return v


FunctionIdStr = Annotated[str, AfterValidator(_validate_function_id)]


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------


class Document(BaseModel):
    id: DocumentIdStr = Field(
        description="Immutable, assigned at creation (short hash + hint from title)."
    )
    title: str = Field(description="Human-readable display name, editable via update_metadata.")
    source_type: SourceType = Field(description="Source artifact format of the original file.")
    source_path: str = Field(
        description="Location of the original artifact (local file path or URI)."
    )
    lifecycle_status: str = Field(
        default="active",
        description=(
            "Current lifecycle state. Uses the base LifecycleStatus values "
            "(active, completed, archived) plus any vault-defined extensions."
        ),
    )
    version_label: str | None = Field(
        default=None,
        description="Human-readable version indicator (v1, v2, draft, final, etc.).",
    )
    project: str | None = Field(
        default=None,
        description="Project or workstream identifier.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Freeform tags, extractable from YAML frontmatter or assigned manually.",
    )
    authority_scope: str | None = Field(
        default=None,
        description="Content domain this document governs, if authoritative.",
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Vault-configured document type (Tier 2 metadata). Values are "
            "defined in the vault's document_types configuration."
        ),
    )
    source_content_hash: Sha256Str = Field(
        description="Hash of source file content at last ingestion (provenance)."
    )
    adapter_version: str = Field(
        description="Version of the source adapter used at last ingestion."
    )
    created_by: str = Field(
        description="User ID of the actor (human or agent) that created this document."
    )
    created_at: datetime = Field(description="Creation timestamp.")
    last_modified_by: str = Field(
        description=(
            "User ID of the actor responsible for the most recent modification. "
            "Set to created_by at document creation; updated on lifecycle "
            "transitions and metadata changes."
        )
    )
    updated_at: datetime = Field(description="Last modification timestamp.")
    projected_at: datetime | None = Field(
        default=None,
        description="Last time the source adapter ran against this source.",
    )
    indexed_at: datetime | None = Field(
        default=None,
        description=(
            "Last time projection was chunked and embedded into content store. "
            "Null until indexing completes (pipeline_status reaches "
            "indexing_complete). Not updated by abstraction (Stage 3)."
        ),
    )
    source_modified_at: datetime | None = Field(
        default=None,
        description=(
            "Modification timestamp of the source file at time of ingestion. "
            "Extracted by source adapters from filesystem metadata (st_mtime). "
            "Null for non-file sources or when the adapter does not provide it."
        ),
    )
    document_date: DocumentDateStr = Field(
        default=None,
        description=(
            "Authoritative content date in YYYY-MM-DD format. Derived from "
            "the filename date code when present, falling back to the date "
            "portion of source_modified_at. Null for non-file sources when "
            "neither is available."
        ),
    )
    semantic_abstract: str | None = Field(
        default=None,
        description=(
            "LLM-generated abstract with density-proportional length. "
            "Generated at ingestion Stage 3, regenerated on re-ingestion. "
            "Null if abstraction is disabled or pending (CAS-ADR-011)."
        ),
    )
    pipeline_status: PipelineStatus = Field(
        default=PipelineStatus.PROJECTION_COMPLETE,
        description="Current state of the three-stage ingestion pipeline.",
    )
    pipeline_error: str | None = Field(
        default=None,
        description=(
            "Failure description when pipeline_status is 'failed'. Null when "
            "pipeline has not failed. Contains the error message from the "
            "failed pipeline stage (projection, indexing, or abstraction)."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Per-doc_type typed metadata payload (Tier 3). Structure varies "
            "by doc_type and is validated against the JSON Schema fragment "
            "declared in vault config for the resolved doc_type."
        ),
    )
    metadata_confirmed: bool = Field(
        default=False,
        description=(
            "True when the document's metadata has been confirmed (no "
            "filename inference needed or inferred values have been reviewed). "
            "False marks the document as pending in the metadata-review queue."
        ),
    )


class DocumentSummary(BaseModel):
    """Compact document representation for list and search results."""

    id: DocumentIdStr = Field(description="Document identifier.")
    title: str = Field(description="Human-readable display name.")
    lifecycle_status: str = Field(description="Current lifecycle state.")
    source_type: SourceType = Field(description="Source artifact format.")
    source_path: str | None = Field(
        default=None, description="Location of the original artifact, when present."
    )
    version_label: str | None = Field(
        default=None, description="Human-readable version indicator, when present."
    )
    project: str | None = Field(
        default=None, description="Project or workstream identifier, when present."
    )
    doc_type: str | None = Field(
        default=None, description="Vault-configured document type, when assigned."
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Freeform tags carried on the document.",
    )
    document_date: datetime | None = Field(
        default=None,
        description="Authoritative content date when available.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        description="Source file modification timestamp at last ingestion.",
    )
    semantic_abstract: str | None = Field(
        default=None,
        description=(
            "LLM-generated semantic abstract of the document content. Present "
            "when the ingestion pipeline completed abstraction; null when "
            "abstraction was skipped or the document predates abstract "
            "generation. Enables steward agents and vault-steward discovery "
            "to access document orientation without a separate get_document "
            "call (CAS-ADR-011)."
        ),
    )


class Edge(BaseModel):
    id: EdgeIdStr = Field(
        description=(
            "Unique edge identifier, auto-generated at creation. Required for "
            "disambiguation when duplicate edges exist between the same "
            "document pair."
        )
    )
    source_id: DocumentIdStr = Field(description="Origin document ID.")
    target_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Target document ID. Null on `retracts` edges, which target an "
            "edge instance rather than a document (see retracted_edge_id)."
        ),
    )
    edge_type: EdgeType = Field(description="Typed relationship between source and target.")
    resolution_policy: ResolutionPolicy | None = Field(
        default=None,
        description=(
            "Effective resolution policy, copied from the vault's "
            "edge_type_registry at edge creation time. Frozen on the row so "
            "later registry edits do not retroactively change resolution "
            "behavior for existing edges."
        ),
    )
    source_valid_from_version: str | None = Field(
        default=None,
        description=(
            "Document ID of the version on the source chain where this edge "
            "becomes applicable. Required for policies `transitive_source` "
            "and `transitive_both`; must be null for policy `none` (except "
            "`retracts`, which carries a one-sided anchor on the retracting "
            "chain)."
        ),
    )
    target_valid_from_version: str | None = Field(
        default=None,
        description=(
            "Document ID of the version on the target chain where this edge "
            "becomes applicable. Required for policy `transitive_both`; "
            "frozen at edge creation for policy `transitive_source` and "
            "copied from target_id; must be null for policy `none`."
        ),
    )
    valid_until_version: str | None = Field(
        default=None,
        description=(
            "Document ID marking the chain head at which this edge is "
            "tombstoned by a `merged_from` termination. Null on a live edge. "
            "Set atomically by the merge operation on predecessor edges. "
            "Resolution ignores tombstoned edges downstream of this version."
        ),
    )
    retracted_edge_id: EdgeIdStr | None = Field(
        default=None,
        description=(
            "Set only on `retracts` edges; identifies the edge instance "
            "being retracted. Null on all other edge types."
        ),
    )
    created_at: datetime = Field(description="Edge creation timestamp.")
    notes: str | None = Field(
        default=None,
        description="Optional annotation on why the relationship exists.",
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Decision rationale for this relationship. Particularly valuable "
            "on supersedes edges for capturing why the transition occurred "
            "(CAS-ADR-011)."
        ),
    )


class User(BaseModel):
    """Any actor (human or agent) registered in the vault.

    Serves provenance tracking and access control.
    """

    id: UserIdStr = Field(description="Immutable, assigned at registration.")
    display_name: str = Field(description="Human-readable name for the user or agent.")
    type: UserType = Field(description="Actor type for provenance and access control.")
    created_at: datetime = Field(description="Registration timestamp.")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    source: str = Field(
        description=(
            "Path or URI to the source artifact. Resolved relative to the vault's storage_root."
        )
    )
    adapter: SourceType = Field(
        description=(
            "Source adapter to use for projection. Must be an enabled "
            "adapter in the vault's source_adapters configuration."
        )
    )
    config: dict | None = Field(
        default=None,
        description=(
            "Adapter-specific configuration overrides. Structure depends on the adapter type."
        ),
    )
    created_by: str | None = Field(
        default=None,
        description=(
            "User ID of the actor initiating ingestion. Used for provenance "
            "tracking. Phase 2+ implementations may derive this from the "
            "authenticated caller identity."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "When true, bypasses duplicate content detection and re-runs the "
            "full pipeline on the existing document record (if one exists "
            "with the same source_path and source_content_hash). Returns 200 "
            "(re-processed) instead of 201 (new). The document ID is "
            "preserved. Use for pipeline failure recovery."
        ),
    )
    needs_review: bool = Field(
        default=False,
        description=(
            "When true, the document is held in the metadata-review queue "
            "after ingest: filename inference runs, parsed values populate "
            "the document where the caller did not supply them, and "
            "metadata_confirmed is set to false so the document appears in "
            "pending-metadata. When false (default per CAS-ADR-021), "
            "filename inference is skipped entirely; the document is "
            "committed with caller-supplied metadata authoritative and "
            "metadata_confirmed=true. Bulk-ingest UIs that want human "
            "confirmation of inferred values pass needs_review=true "
            "explicitly; agent ingests with prepared metadata leave it at "
            "the default."
        ),
    )
    metadata: dict[str, str | list[str]] | None = Field(
        default=None,
        description=(
            "Caller-supplied metadata fields to merge into the document "
            "record at ingestion time. Per CAS-ADR-021, callers are "
            "authoritative for metadata; SAGE applies values per-field with "
            "the precedence chain caller > filename parse (only when "
            "needs_review=true) > chain inherit (predecessor's doc_type, "
            "project, authority_scope when supersedes_document_id is set "
            "and the caller omitted the field) > vault default (doc_type "
            "only, falls through to 'misc'). Fields with null values are "
            "ignored. Unknown field names are stored but have no "
            "schema-enforced semantics. Values are strings except tags, "
            "which may be supplied as a list of strings or as a "
            "comma-separated string (parity with sage_update_metadata's "
            "list-typed tags field)."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Per-doc_type typed metadata payload. Validated at the SAGE API "
            "boundary against the JSON Schema fragment declared in vault "
            "config for the resolved doc_type. When the resolved doc_type "
            "has no metadata_schema declared and tier3_metadata is non-null, "
            "ingest fails with 400 tier3_schema_violation (strict "
            "no-loose-mode per T-0004 design)."
        ),
    )
    supersedes_document_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "When provided, the ingested document is treated as a new "
            "version that supersedes the named predecessor. After successful "
            "ingestion, SAGE applies the `supersede` lifecycle transition on "
            "the predecessor: creates a `supersedes` edge from the new "
            "document to the predecessor and sets the predecessor's "
            "lifecycle_status to `archived`. Predecessor validation (exists "
            "+ active + different content hash) runs before projection; "
            "failures return 404/409 before any pipeline work begins."
        ),
    )

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
    """Inputs for the side-effect-free filename parser endpoint.

    Carries the bare filename (basename, not a full path) and the
    adapter under whose vault configuration the parse should run.
    """

    filename: str = Field(
        description=(
            "Filename to parse. Should be the basename of the source file; "
            "the parser does not consume directory components."
        )
    )
    adapter: SourceType = Field(
        description=(
            "Source adapter whose filename_extraction configuration governs "
            "the parse. Must be an enabled adapter for the vault."
        )
    )


class ParseFilenameResponse(BaseModel):
    """Metadata fields the FilenameParser extracted from the supplied filename.

    All fields are nullable; a field is null when the parser could not
    extract it (no pattern configured, pattern did not match, or the
    captured segment was empty). No document is created and vault state
    is unchanged.
    """

    title: str | None = Field(default=None, description="Title parsed from the filename.")
    project: str | None = Field(
        default=None, description="Project identifier parsed from the filename."
    )
    version_label: str | None = Field(
        default=None, description="Version label parsed from the filename."
    )
    document_date: DocumentDateStr = Field(
        default=None, description="ISO-8601 date string when extractable."
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Resolved doc_type after applying the vault's "
            "keyword_to_doc_type and code_to_doc_type maps over the parsed "
            "segments. Null when no mapping fires."
        ),
    )
    codes: list[str] | None = Field(
        default=None,
        description=(
            "Code segments extracted from the filename via "
            "known_code_patterns or the configured filename pattern. Null "
            "when the parser has no pattern; empty list when the pattern "
            "ran but matched no codes."
        ),
    )


class DocumentWithContent(Document):
    """Document record optionally accompanied by file-delivery information.

    Field population depends on the `get_document` request mode:
    - Default request: all extra fields are None (base Document shape).
    - `include_content=true` (BH-117): `content` (base64 bytes) and
      `content_size` are populated.
    - `write_to_path=<path>` (BH-125): `written_to`, `content_size`, and
      `content_hash` are populated; `content` is None.
    """

    content: str | None = Field(
        default=None,
        description=(
            "Base64-encoded bytes of the vault-local source file at "
            "storage_root/source_path. Present only when the request "
            "specified include_content=true. Decodes to the exact bytes of "
            "the authoritative file."
        ),
    )
    content_size: int | None = Field(
        default=None,
        description=(
            "Byte count of the vault-local source file. Populated when the "
            "request specified include_content=true or write_to_path."
        ),
    )
    content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "Hex digest of the vault-local source file bytes (same algorithm "
            "as Document.source_content_hash). Populated only when the "
            "request specified write_to_path, for caller-side verification "
            "that the on-disk bytes match the vault record."
        ),
    )
    written_to: str | None = Field(
        default=None,
        description=(
            "Absolute path where SAGE wrote the source file bytes. Populated "
            "only when the request specified write_to_path. Equals the "
            "caller-supplied value."
        ),
    )


class SetLifecycleRequest(BaseModel):
    action: str = Field(
        description=(
            "Lifecycle transition action. The action vocabulary is "
            "vault-config-defined; see `lifecycle.transitions` in the vault "
            "config for the authoritative list. Examples seen in practice: "
            "`supersede`, `complete`, `archive`, `reactivate` (cas vault) "
            "plus `file` (pim_health vault). Neither vault defines "
            "`activate`; the action for `archived → active` is `reactivate`."
        )
    )
    new_version_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document ID of the replacement version. Required when "
            "action='supersede'; forbidden for all other actions. SAGE "
            "creates a `supersedes` edge from the new version to this "
            "document atomically with the lifecycle transition. The "
            "successor document must already exist and be active; this "
            "operation does not create it."
        ),
    )


class SetLifecycleResponse(BaseModel):
    """Returned after a successful lifecycle transition.

    Includes the updated document record and optional warnings (e.g.,
    when the ingestion pipeline is still in progress).
    """

    document: Document = Field(description="The document with its updated lifecycle state.")
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Advisory messages about non-blocking concerns. Present when the "
            "document's pipeline_status is non-terminal at the time of the "
            "lifecycle transition. Absent or empty when there are no "
            "warnings."
        ),
    )


def _coerce_tags(v: str | list[str] | None) -> list[str] | None:
    """Accept tags as a comma-separated string or a list of strings."""
    if v is None:
        return None
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    return v


class UpdateMetadataRequest(BaseModel):
    """Partial update of mutable document metadata.

    Only fields present in the request are modified.
    """

    title: str | None = Field(default=None, description="New title; omit to leave unchanged.")
    version_label: str | None = Field(
        default=None, description="New version label; omit to leave unchanged."
    )
    project: str | None = Field(
        default=None, description="New project identifier; omit to leave unchanged."
    )
    tags: Annotated[list[str] | None, BeforeValidator(_coerce_tags)] = Field(
        default=None,
        description=(
            "Replacement tag list; omit to leave unchanged. Accepts a list "
            "of strings or a comma-separated string (the latter is coerced "
            "to a list)."
        ),
    )
    doc_type: str | None = Field(
        default=None,
        description="Must be a valid value in the vault's document_types configuration.",
    )
    authority_scope: str | None = Field(
        default=None,
        description="New authority scope; omit to leave unchanged.",
    )
    document_date: DocumentDateStr = Field(
        default=None,
        description=(
            "Document calendar date (YYYY-MM-DD). Editable to correct "
            "fallback-derived dates that misattributed across UTC midnight."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Per-doc_type typed metadata payload. Top-level replacement "
            "semantics (no deep merge): when supplied, the stored "
            "tier3_metadata dict is replaced wholesale. Validated against "
            "the doc_type's metadata_schema declared in vault config; "
            "400 tier3_schema_violation when invalid or when the doc_type "
            "has no metadata_schema. Omit to leave stored tier3 untouched."
        ),
    )


class RegisterUserRequest(BaseModel):
    display_name: str = Field(description="Human-readable name for the user or agent.")
    type: UserType = Field(description="Actor type for provenance and access control.")


class IngestResponse(BaseModel):
    """Returned by `POST /sage_vaults/{vault_id}/documents`.

    The endpoint is synchronous and returns after the three-stage
    ingestion pipeline completes.

    The REST endpoint is synchronous: `pipeline_status` is at a terminal
    value (`abstraction_complete`, `abstraction_skipped`, or `failed`)
    by the time this response is returned. The MCP `sage_ingest` wrapper
    inverts this and returns early with a non-terminal status; that
    behavior is MCP-specific and does not affect the REST surface.
    """

    document: Document = Field(description="The ingested document record.")
    pipeline_status: PipelineStatus = Field(
        description="Terminal pipeline status reached during the synchronous ingest."
    )


class LinkRequest(BaseModel):
    """Create an edge.

    `target_id` is required for all edge types except `retracts` (which
    uses `retracted_edge_id` instead). Anchor fields
    (`source_valid_from_version`, `target_valid_from_version`) are
    required, optional, or forbidden depending on the edge type's
    `resolution_policy` per CAS-ADR-017. Write-time validation enforces
    the policy-keyed invariant and returns 400 `edge_anchor_policy_violation`
    on violation. Creating an edge whose registry policy is `TBD` returns
    400 `tbd_policy_edge`.
    """

    source_id: DocumentIdStr = Field(description="Origin document ID.")
    target_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Target document ID. Must be null on `retracts` edges; required "
            "for every other edge type."
        ),
    )
    edge_type: EdgeType = Field(description="Typed relationship between source and target.")
    source_valid_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document ID on the source chain where this edge becomes "
            "applicable. Required for policies `transitive_source` and "
            "`transitive_both`; also required (one-sided) for `retracts`. "
            "Must be null for policy `none` on non-retracts edges. Must lie "
            "in the supersedes lineage of source_id (or of the retracting "
            "chain head for `retracts`)."
        ),
    )
    target_valid_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document ID on the target chain where this edge becomes "
            "applicable. Required for policy `transitive_both`; must be null "
            "for policies `transitive_source`, `none`, and for `retracts` "
            "edges. Must lie in the supersedes lineage of target_id when "
            "supplied."
        ),
    )
    retracted_edge_id: EdgeIdStr | None = Field(
        default=None,
        description=(
            "Required for `retracts` edges; must identify an existing edge "
            "in this vault. Must be null for all other edge types."
        ),
    )
    notes: str | None = Field(default=None, description="Optional annotation.")
    rationale: str | None = Field(
        default=None,
        description="Decision rationale for creating this relationship.",
    )


class ResolutionPathEntry(BaseModel):
    """One decision event from the chain-scoped edge resolver (CAS-ADR-017).

    Discriminated by `event_type`.
    """

    event_type: Literal["anchor_hit", "anchor_miss", "retracts_applied", "tombstone_applied"] = (
        Field(
            description=(
                "anchor_hit: the edge's anchor was found in the queried "
                "version's supersedes lineage and the edge surfaced. "
                "anchor_miss: anchor lay outside the lineage; edge "
                "suppressed. retracts_applied: a `retracts` edge in the "
                "queried lineage caused this edge to be suppressed. "
                "tombstone_applied: a `merged_from` tombstone "
                "(valid_until_version set) caused this edge to be "
                "suppressed at the queried version."
            )
        )
    )
    edge_id: EdgeIdStr = Field(description="The edge id whose resolution produced this event.")
    anchor_field: Literal["source_valid_from_version", "target_valid_from_version"] | None = Field(
        default=None,
        description="Which anchor was checked. Populated for anchor_hit and anchor_miss.",
    )
    anchor_version: str | None = Field(
        default=None,
        description=(
            "The document ID stored in the anchor field at the time of the "
            "check. Populated for anchor_hit and anchor_miss."
        ),
    )
    retracted_edge_id: EdgeIdStr | None = Field(
        default=None,
        description=(
            "On retracts_applied: the edge id of the `retracts` edge whose "
            "application produced this event."
        ),
    )
    tombstone_version: str | None = Field(
        default=None,
        description="On tombstone_applied: the valid_until_version that tombstoned the edge.",
    )


class TraverseRequest(BaseModel):
    start_id: DocumentIdStr = Field(description="Document ID to start traversal from.")
    edge_type: EdgeType | None = Field(
        default=None,
        description=("Edge type to follow. If omitted, all edge types are traversed."),
    )
    direction: TraversalDirection = Field(
        default=TraversalDirection.OUTBOUND,
        description="Traversal direction relative to the starting document.",
    )
    depth: int = Field(
        default=3,
        ge=1,
        le=1000,
        description="Maximum traversal depth.",
    )
    debug: bool = Field(
        default=False,
        description=(
            "When true, the response populates `resolution_path` with a "
            "per-event trace of chain-scoped resolution decisions (anchor "
            "hits and misses, retractions applied, tombstones applied). "
            "Opt-in; response is unchanged when false (CAS-ADR-017)."
        ),
    )


class TraversalNode(BaseModel):
    """A node encountered during graph traversal.

    Carries the edge that led to it and the depth at which it was found.
    When multiple edges connect to the same target, the target appears
    once with the most recent edge shown and edge_counts providing
    per-type totals.
    """

    document: DocumentSummary = Field(
        description="Compact summary of the document encountered at this node."
    )
    edge: Edge = Field(description="The most recent edge leading to this document (by created_at).")
    depth: int = Field(description="Traversal depth (1 = direct neighbor of start node).")
    edge_counts: dict[str, int] = Field(
        default_factory=lambda: {},
        description=(
            "Edge count by type. Keys are edge type strings, values are the "
            "number of edges of that type connecting to this document from "
            "the traversal path. When the traversal is filtered by "
            "edge_type, only the filtered type(s) appear as keys."
        ),
    )


class TraverseResponse(BaseModel):
    start_id: DocumentIdStr = Field(description="The document ID traversal started from.")
    nodes: list[TraversalNode] = Field(
        description="Nodes reached by the traversal, ordered by visit sequence."
    )
    resolution_path: list[ResolutionPathEntry] | None = Field(
        default=None,
        description=(
            "Per-event trace of chain-scoped resolution decisions. Present "
            "only when the request set `debug: true`; otherwise null / "
            "omitted. Useful for diagnosing why an expected edge was "
            "suppressed or why an unexpected edge surfaced."
        ),
    )


class ChainRequest(BaseModel):
    document_id: DocumentIdStr = Field(
        description=(
            "Document ID to start the chain walk from. The walk proceeds in "
            "both directions from this document."
        )
    )
    edge_type: EdgeType = Field(
        description=(
            "Edge type to follow. Required (no default). The chain walk "
            "follows only this edge type in both directions."
        )
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of entries to return; null returns the full chain.",
    )
    offset: int = Field(
        default=0,
        description="Number of entries to skip from the head before returning.",
    )


class ChainEntry(BaseModel):
    id: DocumentIdStr = Field(description="Document ID.")
    title: str = Field(description="Document title.")
    version_label: str | None = Field(default=None, description="Version label when assigned.")
    lifecycle_status: str = Field(description="Lifecycle state of this chain entry.")
    document_date: DocumentDateStr = Field(
        default=None,
        description="Authoritative content date (YYYY-MM-DD) if available.",
    )
    position: int = Field(
        description="Zero-based ordinal position in the chain (0 = tail, length-1 = head)."
    )


class ChainResponse(BaseModel):
    chain: list[ChainEntry] = Field(
        description=(
            "Ordered list of documents in the chain, from tail (position 0) "
            "to head (position length-1)."
        )
    )
    head_id: DocumentIdStr = Field(
        description="Document ID of the chain head (latest version / terminal node)."
    )
    tail_id: DocumentIdStr = Field(
        description="Document ID of the chain tail (original / root node)."
    )
    query_position: int = Field(
        description="Zero-based position of the queried document within the chain."
    )
    length: int = Field(description="Number of entries actually returned in this response.")
    total_length: int = Field(description="Total number of documents in the full chain.")
    is_linear: bool = Field(
        description=(
            "True if the chain is strictly linear (no forks or merges). "
            "False indicates a data quality issue where multiple documents "
            "share a predecessor or successor of the specified edge type."
        )
    )
    available_edge_types: list[str] | None = Field(
        default=None,
        description=(
            "Other edge types observed on the queried document, surfaced as "
            "a hint when the requested edge_type produced no chain."
        ),
    )


class PreconditionCheck(BaseModel):
    target_id: DocumentIdStr = Field(description="Document ID of the dependency target.")
    required: str = Field(
        description=(
            "Required condition (e.g., 'exists and lifecycle_status in [active, completed]')."
        )
    )
    actual: str = Field(description="Actual state found (e.g., 'active', 'not found').")
    satisfied: bool = Field(description="Whether this individual check is satisfied.")


class PreconditionResult(BaseModel):
    function_id: FunctionIdStr = Field(
        description="The function document whose preconditions were checked."
    )
    satisfied: bool = Field(description="True if all dependencies are satisfied.")
    checks: list[PreconditionCheck] = Field(description="Per-dependency check results.")


# ---------------------------------------------------------------------------
# Discover (retrieval) models
# ---------------------------------------------------------------------------


class RetrievalFilters(BaseModel):
    """Metadata filters applied before retrieval.

    Used when scope is "filtered", but also applicable as additional
    constraints with other scopes.
    """

    doc_type: str | None = Field(
        default=None, description="Filter by vault-configured document type."
    )
    project: str | None = Field(default=None, description="Filter by project identifier.")
    lifecycle_status: str | None = Field(default=None, description="Filter by lifecycle state.")
    tags: list[str] | None = Field(
        default=None,
        description="Filter by tags (documents must have all specified tags).",
    )
    document_ids: list[str] | None = Field(
        default=None,
        description="Restrict to a specific set of documents. Used with scope 'specific'.",
    )
    pipeline_status: str | None = Field(
        default=None,
        description=(
            "Filter by pipeline status (e.g., 'failed', "
            "'abstraction_complete'). Overrides the default exclusion of "
            "failed-pipeline documents."
        ),
    )
    tier3: dict | None = Field(
        default=None,
        description=(
            "Tier 3 (per-doc_type typed metadata) post-filter. Each "
            "key/value pair in the dict is matched against the document's "
            "`tier3_metadata` dict via exact equality. A value of None "
            "matches documents whose stored field is either null or absent "
            "from the tier3_metadata dict. All pairs AND together; an empty "
            "dict is treated as no filter."
        ),
    )


class DiscoverRequest(BaseModel):
    """Retrieval request. Required fields vary by mode.

    - semantic: query is required.
    - keyword: query is required. Use query="*" for filter-only listing.
    - catalog: no query required. Returns document-level metadata only
      (no chunks or scores). Queries SQLite directly with filter
      predicates. Supports pagination via limit + offset.
    - deterministic: document_id and heading_path are required.
    """

    mode: RetrievalMode = Field(
        default=RetrievalMode.SEMANTIC,
        description="Retrieval mode selecting the underlying query strategy.",
    )
    query: str | None = Field(
        default=None,
        description=(
            "Search query text. Required for semantic and keyword modes. Ignored in catalog mode."
        ),
    )
    scope: RetrievalScope = Field(
        default=RetrievalScope.ALL,
        description="Controls which documents are eligible for retrieval.",
    )
    filters: RetrievalFilters | None = Field(
        default=None,
        description="Metadata filters applied before retrieval.",
    )
    document_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Target document for deterministic extraction. Required for deterministic mode."
        ),
    )
    heading_path: str | None = Field(
        default=None,
        description=(
            "Heading hierarchy path for deterministic extraction (e.g., "
            "'Section 3 > Definitions > Normalization'). Required for "
            "deterministic mode."
        ),
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of results to return.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of results to skip before returning. Used with limit "
            "for pagination in catalog mode. Ignored by other modes."
        ),
    )
    use_hybrid: bool = Field(
        default=True,
        description=(
            "Enable hybrid retrieval (vector + BM25 fusion) in semantic "
            "mode. Valuable when queries contain both natural language and "
            "specific identifiers. Set to false for pure vector similarity "
            "search."
        ),
    )
    use_abstract_prefilter: bool = Field(
        default=True,
        description=(
            "Enable two-pass abstract-boosted retrieval (CAS-ADR-011). When "
            "true, documents whose semantic abstract matches the query "
            "receive a score boost above documents whose abstract does not "
            "match. Applies to semantic and keyword modes. Documents "
            "without abstracts are not excluded. Set to false for pure "
            "content-only relevance ranking."
        ),
    )
    include_abstracts: bool = Field(
        default=False,
        description=(
            "When true, populates the semantic_abstract field on each result "
            "document so callers can render orientation without a separate "
            "get_document call."
        ),
    )
    min_relevance: float | None = Field(
        default=None,
        description=(
            "Minimum relevance score threshold; results below this score "
            "are dropped. Applicable to semantic mode."
        ),
    )
    response_level: ResponseLevel = Field(
        default=ResponseLevel.CHUNKS,
        description=(
            "Controls result detail level. 'chunks' (default) includes "
            "chunk_content, heading_path, and matched_chunk_count. "
            "'documents' suppresses chunk_content but preserves "
            "heading_path (best chunk's location) and matched_chunk_count "
            "as lightweight context. Applicable to semantic and keyword "
            "modes. Ignored by catalog (always document-level) and "
            "deterministic (always chunk-level)."
        ),
    )
    sort_by: CatalogSortBy | None = Field(
        default=None,
        description=(
            "Sort key for catalog mode results. Optional. Default when "
            "omitted: active lifecycle first, then document_date descending. "
            "Ignored by other retrieval modes."
        ),
    )
    sort_order: SortOrder | None = Field(
        default=None,
        description=(
            "Sort direction for catalog mode results. Optional. Defaults to "
            "ascending when sort_by is specified. Ignored by other modes."
        ),
    )


class DiscoverHit(BaseModel):
    """A single retrieval result. Fields populated depend on the retrieval mode."""

    document: DocumentSummary = Field(description="Compact summary of the matching document.")
    chunk_content: str | None = Field(
        default=None,
        description=(
            "Retrieved text content. For semantic mode, the matching chunk. "
            "For deterministic mode, the extracted content at the specified "
            "heading path."
        ),
    )
    heading_path: str | None = Field(
        default=None,
        description=(
            "Heading hierarchy path of the retrieved chunk (e.g., 'Section "
            "3 > Definitions > Normalization')."
        ),
    )
    relevance_score: float | None = Field(
        default=None,
        description=(
            "Relevance score (semantic mode only). Higher is more relevant. "
            "Scale depends on the retrieval implementation."
        ),
    )
    matched_chunk_count: int | None = Field(
        default=None,
        description=(
            "Number of chunks from this document that matched the query. "
            "Present in semantic and keyword modes. A document with many "
            "matching chunks is a stronger hit than one with a single match "
            "at a similar peak score. Useful as a reranking signal."
        ),
    )


class DiscoverResponse(BaseModel):
    mode: RetrievalMode = Field(description="The retrieval mode that produced these results.")
    results: list[DiscoverHit] = Field(description="The ranked retrieval results.")
    total_available: int = Field(
        description=(
            "Total number of results available (before pagination). May be "
            "approximate for semantic mode."
        )
    )
    hints: dict[str, object] | None = Field(
        default=None,
        description=(
            "Optional retrieval hints surfaced to the caller (e.g., "
            "suggestions when results are sparse). Null when no hints apply."
        ),
    )


# ---------------------------------------------------------------------------
# Utility models
# ---------------------------------------------------------------------------


class ExportProjectionRequest(BaseModel):
    output_path: str = Field(
        description=(
            "File path to write the projection Markdown file. Resolved "
            "relative to the vault's storage_root."
        )
    )


class ExportProjectionResponse(BaseModel):
    document_id: DocumentIdStr = Field(description="The document whose projection was exported.")
    output_path: str = Field(description="Absolute path where the projection file was written.")


class ReadProjectionResponse(BaseModel):
    document_id: DocumentIdStr = Field(description="Document identifier.")
    title: str = Field(description="Document title.")
    version_label: str | None = Field(default=None, description="Version label when assigned.")
    lifecycle_status: str = Field(description="Current lifecycle state.")
    doc_type: str | None = Field(
        default=None, description="Vault-configured document type when assigned."
    )
    source_path: str = Field(description="Location of the original artifact.")
    projection_text: str = Field(
        description="Full canonical projection text reassembled from stored chunks."
    )


class ReadSectionResponse(BaseModel):
    document_id: DocumentIdStr = Field(description="Document identifier.")
    title: str = Field(description="Document title.")
    heading_path: str = Field(description="Heading hierarchy path that was read.")
    chunk_count: int = Field(description="Number of chunks matched by the heading_path prefix.")
    section_text: str = Field(description="Concatenated text of the matching chunks.")


class AssertionFailure(BaseModel):
    query: str = Field(description="The assertion query that failed.")
    expected_document_id: DocumentIdStr = Field(
        description="Document ID expected in the top-k results."
    )
    top_k_checked: int = Field(description="How many top results were checked.")
    found: bool = Field(description="Whether the expected document appeared in top-k.")
    actual_rank: int | None = Field(
        default=None,
        description=(
            "Rank at which the expected document appeared, if found outside "
            "the assertion's top-k threshold."
        ),
    )


class RefreshViewsResponse(BaseModel):
    vault_id: VaultIdStr = Field(description="The vault whose views were refreshed.")
    views_generated: int = Field(description="Number of view artifacts regenerated.")


class EvalRetrievalResult(BaseModel):
    vault_id: VaultIdStr = Field(description="The vault that was evaluated.")
    passed: bool = Field(description="True if all assertions passed.")
    assertion_count: int = Field(description="Total number of assertions evaluated.")
    failure_count: int = Field(description="Number of assertions that failed.")
    failures: list[AssertionFailure] = Field(description="Detail for each failing assertion.")


# ---------------------------------------------------------------------------
# Vault listing and statistics (BE-001 through BE-006)
# ---------------------------------------------------------------------------


class VaultDocTypeEntry(BaseModel):
    value: str = Field(description='Doc type identifier (e.g. "patent_draft").')
    label: str = Field(description="Human-readable label for UI display.")


class VaultLifecycleState(BaseModel):
    value: str = Field(description="Lifecycle state identifier.")
    label: str = Field(description="Human-readable label for UI display.")
    is_terminal: bool = Field(
        default=False,
        description="Terminal states cannot be transitioned out of.",
    )


class VaultAdapterInfo(BaseModel):
    source_type: str = Field(description="Source artifact format the adapter handles.")
    enabled: bool = Field(
        description=(
            "Whether the adapter is enabled in the vault config. Disabled "
            "adapters surface in scan results as status 'adapter_disabled'."
        )
    )
    extensions: list[str] = Field(
        description='File extensions handled by this adapter (e.g. [".md", ".markdown"]).'
    )


class VaultSummary(BaseModel):
    id: VaultIdStr = Field(description="Vault identifier (path segment under ~/sage_vaults/).")
    name: str = Field(description="Human-readable vault name.")
    description: str | None = Field(
        default=None, description="Optional vault description from the vault config."
    )
    storage_root: str = Field(description="Path to the vault's source-file storage root.")
    doc_types: list[VaultDocTypeEntry] = Field(
        default_factory=list,
        description="Vault-configured document types.",
    )
    lifecycle_states: list[VaultLifecycleState] = Field(
        default_factory=list,
        description="Lifecycle states defined by the vault config.",
    )
    adapters: list[VaultAdapterInfo] = Field(
        default_factory=list,
        description="Source adapters configured for the vault.",
    )
    projects: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct project identifiers observed across the vault's "
            "documents. Derived from the project metadata field at response "
            "time."
        ),
    )


class HealthIndicators(BaseModel):
    pending_metadata_count: int = Field(
        description="Documents whose extracted metadata is unconfirmed."
    )
    pending_edge_count: int = Field(description="Tier 2 staging edges awaiting review.")
    deferred_abstract_count: int | None = Field(
        description=(
            "Documents with pipeline_status=abstraction_skipped. Null when "
            "abstraction is disabled in the vault config."
        )
    )
    failed_ingestion_count: int = Field(description="Documents with pipeline_status=failed.")


class VaultStatsResponse(BaseModel):
    total_documents: int = Field(description="Total document count in the vault.")
    by_lifecycle_state: dict[str, int] = Field(
        description="Document count keyed by lifecycle state."
    )
    by_doc_type: dict[str, int] = Field(description="Document count keyed by doc_type.")
    by_source_adapter: dict[str, int] = Field(description="Document count keyed by source adapter.")
    total_edges: int = Field(description="Total edge count in the vault.")
    by_edge_type: dict[str, int] = Field(description="Edge count keyed by edge type.")
    staging_edge_count: int = Field(description="Count of Tier 2 staging edges awaiting review.")
    lancedb_size_bytes: int = Field(description="On-disk size of the LanceDB directory in bytes.")
    lancedb_chunk_count: int = Field(description="Total chunk row count across all documents.")
    sqlite_size_bytes: int = Field(description="Size of the graph.db SQLite file in bytes.")
    last_ingestion_at: datetime | None = Field(
        description="Timestamp of the most recent successful ingestion, if any."
    )
    health: HealthIndicators = Field(description="Vault-level health indicators.")


# ---------------------------------------------------------------------------
# Hash check (BE-007 through BE-009)
# ---------------------------------------------------------------------------


class HashCheckRequest(BaseModel):
    hashes: list[Sha256Str] = Field(description="SHA-256 hex digests to check for existence.")


class HashCheckMatch(BaseModel):
    exists: bool = Field(description="True when a document with the queried hash is present.")
    document_id: DocumentIdStr | None = Field(
        default=None,
        description="Document id when exists=true; null otherwise.",
    )


# ---------------------------------------------------------------------------
# Vault config endpoints (PUT /sage_vaults/{vault_id}/config, POST /sage_vaults)
# ---------------------------------------------------------------------------


class UpdateVaultConfigRequest(BaseModel):
    """Section-level config update.

    Each provided top-level section replaces the current section
    wholesale; omitted sections are preserved unchanged. Section
    structure follows docs/fs/sage/vault_config.schema.json.
    """

    vault: dict | None = Field(default=None, description="Replacement for the `vault` section.")
    document_types: dict | None = Field(
        default=None, description="Replacement for the `document_types` section."
    )
    lifecycle: dict | None = Field(
        default=None, description="Replacement for the `lifecycle` section."
    )
    source_adapters: dict | None = Field(
        default=None, description="Replacement for the `source_adapters` section."
    )
    metadata_extraction: dict | None = Field(
        default=None, description="Replacement for the `metadata_extraction` section."
    )
    edge_inference: dict | None = Field(
        default=None, description="Replacement for the `edge_inference` section."
    )
    abstraction: dict | None = Field(
        default=None, description="Replacement for the `abstraction` section."
    )
    access_control_defaults: dict | None = Field(
        default=None,
        description="Replacement for the `access_control_defaults` section.",
    )
    retrieval_health: dict | None = Field(
        default=None, description="Replacement for the `retrieval_health` section."
    )


class CreateVaultRequest(BaseModel):
    """Full config dict for new vault creation."""

    config: dict = Field(
        description=(
            "Full vault configuration object. Structure defined by "
            "docs/fs/sage/vault_config.schema.json."
        )
    )


# ---------------------------------------------------------------------------
# Staging edges (BE-010 through BE-013)
# ---------------------------------------------------------------------------


class StagingEdge(BaseModel):
    id: EdgeIdStr = Field(description="Staging edge identifier.")
    source_id: DocumentIdStr = Field(description="Origin document of the proposed edge.")
    target_id: DocumentIdStr = Field(description="Target document of the proposed edge.")
    edge_type: EdgeType = Field(description="Proposed edge type.")
    inference_evidence: str = Field(
        description=(
            "Free-text rationale produced by the inference strategy that "
            "proposed this edge (e.g. \"filename code 'PV07' matches covered "
            'document").'
        )
    )
    confidence_tier: int = Field(
        default=2,
        description="Inference tier (Tier 2 = staged for review).",
    )
    created_at: datetime = Field(description="Staging timestamp.")


# ---------------------------------------------------------------------------
# Pending metadata (BE-014 through BE-015)
# ---------------------------------------------------------------------------


class ExtractedField(BaseModel):
    value: str | None = Field(
        default=None, description="The extracted value, or null if unavailable."
    )
    source: str = Field(
        description="How this field was derived ('filename', 'content', or 'default')."
    )
    alt_value: str | None = Field(default=None, description="Optional alternative candidate value.")
    alt_source: str | None = Field(default=None, description="Source label for alt_value.")


class PendingMetadataItem(BaseModel):
    document: Document = Field(description="The document awaiting metadata confirmation.")
    extracted_fields: dict[str, ExtractedField] = Field(
        description=(
            "Per-field annotation. Keys include 'title', 'doc_type', "
            "'project', 'tags', 'document_date' depending on which fields "
            "were extracted."
        )
    )


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Uniform error envelope for all endpoints."""

    code: str = Field(
        description=(
            "Machine-readable error code (e.g., 'invalid_lifecycle_transition', "
            "'document_not_found', 'editor_permission_denied')."
        )
    )
    message: str = Field(description="Human-readable error description.")
    detail: dict | None = Field(
        default=None,
        description=(
            "Additional context. Structure varies by error type (e.g., "
            "current_state and attempted_action for lifecycle transition "
            "errors)."
        ),
    )
