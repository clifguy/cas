"""Pydantic v2 models derived from the SAGE Core API OpenAPI specification.

lifecycle_status is str (not enum) because vaults define domain-specific
extensions like 'filed' that aren't in the base enum.
"""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from sage.models.enums import (
    CatalogSortBy,
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ReabstractOutcome,
    ResolutionPolicy,
    ResponseMode,
    RetrievalMode,
    RetrievalScope,
    RetrievalTarget,
    SortOrder,
    SourceType,
    StalenessBasis,
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
# Shared response primitives
# ---------------------------------------------------------------------------


class FieldChange(BaseModel):
    """A single field-level delta inside a dry-run response.

    Used by `UpdateMetadataResponse`, `SetLifecycleResponse`,
    `BulkLifecycleItemResult`, and `BulkMetadataItemResult` to enumerate
    the would-be deltas a dry-run would persist on a real run.

    `path` uses dotted notation for nested keys. Top-level scalar fields
    use the bare field name (`"title"`, `"lifecycle_status"`); nested
    keys inside `tier3_metadata` use the dotted form
    (`"tier3_metadata.severity"`). Collection fields like `tags` carry
    the full ordered before/after lists in `before` / `after`, not patch
    operations — callers can compute the add/remove diff themselves.

    `before` is the pre-patch value; `None` when the path was absent
    before the patch. `after` is the would-be post-patch value; `None`
    when the path is removed by the patch (e.g., a `tier3_metadata`
    unset).

    Populated only on dry-run responses. Real-run responses carry
    `changes is None`. Non-None `changes` therefore unambiguously means
    "this was a dry-run that computed deltas."
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "Dotted path to the changed field (e.g., `title`, "
            "`lifecycle_status`, `tier3_metadata.severity`, `tags`)."
        )
    )
    before: Any = Field(
        default=None,
        description=(
            "Pre-patch value at `path`, or null if the path was absent "
            "before the patch. Type matches the underlying field "
            "(string, list, object, etc.)."
        ),
    )
    after: Any = Field(
        default=None,
        description=(
            "Would-be post-patch value at `path`, or null if the path "
            "is removed by the patch. Type matches the underlying field."
        ),
    )


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
            "Current lifecycle state. Uses the base LifecycleStatus "
            "enum plus any vault-defined extensions."
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
            'Failure description when pipeline_status is "failed". Null '
            "when pipeline has not failed. Contains the error message "
            "from the failed pipeline stage (projection, indexing, or "
            "abstraction)."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Source-type-specific metadata (Tier 3). Structure varies "
            "by source_type. Stored as key-value pairs in the graph "
            "store."
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

    id: DocumentIdStr = Field(description="Unique document identifier.")
    title: str = Field(description="Human-readable document title.")
    lifecycle_status: str = Field(
        description="Current lifecycle state of the document (active, archived, etc.)."
    )
    source_type: SourceType = Field(description="Source artifact format.")
    source_path: str | None = Field(
        default=None, description="Location of the original artifact, when present."
    )
    version_label: str | None = Field(
        default=None,
        description=(
            'Caller-supplied version label for the document (e.g. "v1.2"); '
            "null when no version is tracked."
        ),
    )
    project: str | None = Field(
        default=None,
        description=(
            "Project the document belongs to within the vault's domain; null when unscoped."
        ),
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Vault-domain document type from the doc_types vocabulary; null when not classified."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Caller-supplied tags applied to the document.",
    )
    document_date: DocumentDateStr = Field(
        default=None,
        description=(
            "Authoritative content date in YYYY-MM-DD format (calendar date, "
            "not a UTC-anchored instant). Matches the on-disk Document.document_date "
            "shape — the projection passes the string through unchanged so the wire "
            "serialization stays a bare calendar date. Consumers that need a "
            "datetime (e.g., recency scoring) parse at the use site via "
            "sage.utils.date_parsing.parse_document_date."
        ),
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
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Typed per-doc_type metadata (CAS-ADR-028). Opaque dict whose "
            "key set is determined by the document's doc_type tier3 schema; "
            "consumers should probe for keys defensively because the shape "
            "varies by doc_type. Null when the document carries no tier3 "
            "metadata, either because the doc_type declares no schema or "
            "because no values have been set. Surfaced on the projection "
            "(T-0090) so callers can read fields like ticket_priority or "
            "failure_class from a single catalog/semantic/keyword pass "
            "without follow-up get_document round-trips."
        ),
    )

    @classmethod
    def from_document(cls, doc: "Document") -> "DocumentSummary":
        """Build a DocumentSummary from a Document.

        Single owner of the Document → DocumentSummary projection. Adding a
        field to DocumentSummary requires updating exactly one site; the
        exhaustive-fields test in tests/sage/test_retrieval.py fails closed
        if a future field is added to the schema but not to this factory.
        """
        # Normalize document_date to the strict YYYY-MM-DD calendar-date
        # string. Legacy records on disk may carry ISO-shape or malformed
        # values that the strict DocumentDateStr validator on the projection
        # field would reject; parse_document_date tolerantly handles those
        # (returning None for the truly unparseable), and strftime emits the
        # canonical calendar shape. The factory encapsulates this read-path
        # tolerance so the projection's published contract stays strict.
        from sage.utils.date_parsing import parse_document_date

        parsed_dd = parse_document_date(doc.document_date)
        document_date = parsed_dd.strftime("%Y-%m-%d") if parsed_dd else None

        return cls(
            id=doc.id,
            title=doc.title,
            lifecycle_status=doc.lifecycle_status,
            source_type=doc.source_type,
            source_path=doc.source_path,
            version_label=doc.version_label,
            project=doc.project,
            doc_type=doc.doc_type,
            tags=doc.tags,
            document_date=document_date,
            source_modified_at=doc.source_modified_at,
            semantic_abstract=doc.semantic_abstract,
            tier3_metadata=doc.tier3_metadata,
        )

    @classmethod
    def from_traversal_row(cls, row: dict) -> "DocumentSummary":
        """Build a DocumentSummary from a graph-traversal CTE row dict.

        Single owner of the CTE-row → DocumentSummary projection per the
        *CAS Projection-Point Audit Conventions* steering document (cas
        vault, doc_type=steering_document). The exhaustive-fields test
        ``test_from_traversal_row_populates_every_document_summary_field``
        in ``tests/sage/test_graph_ops.py`` fails closed if a field is
        added to DocumentSummary but not wired through this factory.

        Sibling to ``from_document``: traversal hits flow through this
        path on the BH-101 hot path (per the *Projection-Point Closure
        Cohort — Canonical Decisions* reference document, routes
        directly from the row dict rather than reconstructing a Document
        per row). The two factories project to the same model from
        different source shapes; each owns its own exhaustive-fields
        test.

        Nullable storage-layer columns that may not be present on every
        CTE row (``d_source_path``, ``d_document_date``,
        ``d_source_modified_at``, ``d_semantic_abstract``,
        ``d_tier3_metadata``) are read via ``dict.get`` so older row
        shapes that pre-date column additions continue to project None
        without raising; the closure test populates every key non-null
        so factory-side drift on field additions is still surfaced.
        """
        import json

        # Normalize document_date to the strict YYYY-MM-DD calendar-date
        # string — same shape as from_document; see the comment there for
        # the read-path tolerance rationale.
        from sage.utils.date_parsing import parse_document_date

        source_modified_at_raw = row.get("d_source_modified_at")
        tags_raw = row.get("d_tags")
        parsed_dd = parse_document_date(row.get("d_document_date"))
        document_date = parsed_dd.strftime("%Y-%m-%d") if parsed_dd else None
        return cls(
            id=row["doc_id"],
            title=row["d_title"],
            lifecycle_status=row["d_lifecycle_status"],
            source_type=SourceType(row["d_source_type"]),
            source_path=row.get("d_source_path"),
            version_label=row["d_version_label"],
            project=row["d_project"],
            doc_type=row["d_doc_type"],
            tags=json.loads(tags_raw) if tags_raw else [],
            document_date=document_date,
            source_modified_at=(
                datetime.fromisoformat(source_modified_at_raw) if source_modified_at_raw else None
            ),
            semantic_abstract=row.get("d_semantic_abstract"),
            tier3_metadata=row.get("d_tier3_metadata"),
        )


class DocumentSummaryLight(BaseModel):
    """Stripped DocumentSummary returned by ``search`` with
    ``target="documents", mode="catalog", response_mode="light"``.

    Carries only the identity columns plus the two fields most callers
    need for triage (``doc_type`` and ``tier3_metadata``). Other
    DocumentSummary fields (source_type, source_path, version_label,
    project, tags, document_date, source_modified_at, semantic_abstract)
    are intentionally absent to keep bulk catalog enumerations inside
    the MCP inline-output budget. Callers who need the omitted fields
    pass ``response_mode="full"`` (returns ``DocumentSummary``) or fall
    back to ``get_document`` per id.
    """

    id: DocumentIdStr = Field(description="Unique document identifier.")
    title: str = Field(description="Human-readable document title.")
    lifecycle_status: str = Field(
        description="Current lifecycle state of the document (active, archived, etc.)."
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Vault-domain document type from the doc_types vocabulary; null when not classified."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Typed per-doc_type metadata (CAS-ADR-028). Opaque dict whose "
            "key set is determined by the document's doc_type tier3 schema."
        ),
    )

    @classmethod
    def from_document(cls, doc: "Document") -> "DocumentSummaryLight":
        """Build a DocumentSummaryLight from a Document."""
        return cls(
            id=doc.id,
            title=doc.title,
            lifecycle_status=doc.lifecycle_status,
            doc_type=doc.doc_type,
            tier3_metadata=doc.tier3_metadata,
        )


class Edge(BaseModel):
    id: EdgeIdStr = Field(
        description=(
            "Unique edge identifier, auto-generated at creation. Per T-0079, "
            "the production edges table enforces UNIQUE (source_id, target_id, "
            "edge_type), so at most one edge per natural-key triple ever exists; "
            "the id is still per-edge for retracted_edge_id targeting and for "
            "audit trails."
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
            "edge_type_registry at edge creation time. Frozen on the "
            "row so later registry edits do not retroactively change "
            "resolution behavior for existing edges. Null when an Edge "
            "is constructed without policy resolution (e.g., the "
            "traversal-node Edge views returned by traverse)."
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
    created_at: datetime = Field(description="Timestamp when the edge was created.")
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
    rationale_kind: RationaleKind = Field(
        default=RationaleKind.MANUAL,
        description=(
            "Typed discriminator for the rationale's provenance source. "
            "Promoted from the rationale-text prefix convention in "
            "CAS-ADR-019 to a typed, indexed column (T-0080). Auto- "
            "inference paths stamp this explicitly; hand-curated edges "
            "and legacy rows take the default `manual`."
        ),
    )
    synced_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "The source-chain version (document id) the content was "
            "copied or derived from at the moment this edge was "
            "asserted. Semantically meaningful on `sync_target` (Tier 1, "
            "populated automatically at re-ingestion when the Tier-1 "
            "inference subsystem ships) and `derived_from` (Tier 3, "
            "agent-supplied via create_edge). Distinct from "
            "`source_valid_from_version`, which records chain-scoped "
            "edge visibility per CAS-ADR-017 — the two must not be "
            "conflated. Unset = explicit null; never inferred from chain "
            "anchors. (T-0110 schema; T-0111 typed)"
        ),
    )
    synced_from_content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "The source document's `source_content_hash` captured at "
            "the moment this edge was asserted. Optional companion to "
            "`synced_from_version`; recommended on derivations because "
            "version labels are reused and can drift from content "
            "(in-place edits). Must match `^sha256:[0-9a-f]{64}$`; "
            "unset = explicit null. (T-0110 schema; T-0111 typed)"
        ),
    )


class User(BaseModel):
    """Any actor (human or agent) registered in the vault.

    Serves provenance tracking and access control.
    """

    id: UserIdStr = Field(description="Immutable, assigned at registration.")
    display_name: str = Field(
        description="Human-readable name for the user (shown in audit logs and UIs)."
    )
    user_type: UserType = Field(description="Actor type for provenance and access control.")
    created_at: datetime = Field(description="Timestamp when the user was registered.")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    source: str = Field(
        description=(
            "Path or URI to the source artifact. Resolved relative to the vault's storage_root."
        )
    )
    source_type: SourceType = Field(
        description=(
            "Source artifact format. Determines which source adapter "
            "processes the artifact. Must be an enabled adapter in the "
            "vault's source_adapters configuration."
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
            "project, authority_scope when predecessor_id is set "
            "and the caller omitted the field) > vault default (doc_type "
            "only, falls through to 'misc'). Fields with null values are "
            "ignored. Unknown field names are stored but have no "
            "schema-enforced semantics. Values are strings except tags, "
            "which may be supplied as a list of strings or as a "
            "comma-separated string (parity with update_metadata's "
            "list-typed tags field)."
        ),
    )
    predecessor_id: DocumentIdStr | None = Field(
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
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Caller-authoritative tier-3 typed metadata applied at "
            "create time per CAS-ADR-021. Validated against the "
            "doc_type's `metadata_schema` declared in vault config "
            "(`document_types.{doc_type}.metadata_schema`); if the "
            "doc_type has no schema declared, the ingest fails with "
            "400 `tier3_schema_violation`. Bare-dict form: the entire "
            "object is the tier-3 metadata for the new document. This "
            "is the ingest-vs-update shape asymmetry called out in "
            "CAS-ADR-028: ingest takes the literal dict, whereas "
            "`UpdateMetadataRequest.tier3_metadata` takes a "
            "`Tier3Patch` ops-object with `set`/`unset` semantics."
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
    source_type under whose vault configuration the parse should run.
    """

    filename: str = Field(
        description=(
            "Filename to parse. Should be the basename of the source file; "
            "the parser does not consume directory components."
        )
    )
    source_type: SourceType = Field(
        description=(
            "Source artifact format whose filename_extraction "
            "configuration governs the parse. Must be an enabled "
            "source adapter for the vault."
        )
    )


class ParseFilenameResponse(BaseModel):
    """Metadata fields the FilenameParser extracted from the supplied filename.

    All fields are nullable; a field is null when the parser could not
    extract it (no pattern configured, pattern did not match, or the
    captured segment was empty). No document is created and vault state
    is unchanged.
    """

    title: str | None = Field(
        default=None,
        description="Title segment extracted from the filename; null when no title was captured.",
    )
    project: str | None = Field(
        default=None,
        description=(
            "Project code extracted from the filename via "
            "project_identifier; null when none captured."
        ),
    )
    version_label: str | None = Field(
        default=None,
        description="Version label extracted from the filename; null when none captured.",
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
            "`storage_root/source_path`. Present only when the request "
            "specified `include_content=true`. Decodes to the exact "
            "bytes of the authoritative file."
        ),
    )
    content_size: int | None = Field(
        default=None,
        description=(
            "Byte count of the vault-local source file. Populated when "
            "the request specified `include_content=true` or "
            "`write_to_path`."
        ),
    )
    content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "Hex digest of the vault-local source file bytes (same algorithm "
            "as Document.source_content_hash). Populated only when the request "
            "specified `write_to_path`, for caller-side verification that the "
            "on-disk bytes match the vault record."
        ),
    )
    written_to: str | None = Field(
        default=None,
        description=(
            "Absolute path where SAGE wrote the source file bytes. "
            "Populated only when the request specified `write_to_path`. "
            "Equals the caller-supplied value."
        ),
    )


class SetLifecycleRequest(BaseModel):
    action: str = Field(
        description=(
            "Lifecycle transition action. The action vocabulary is "
            "vault-config-defined; see `lifecycle.transitions` in the vault "
            "config for the authoritative list. Examples seen in practice: "
            "`supersede`, `complete`, `archive`, `reactivate` (cas vault) "
            "plus `file` (example_vault vault). Neither vault defines "
            "`activate`; the action for `archived → active` is `reactivate`."
        )
    )
    successor_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document ID of the replacement version. Required when "
            '`action="supersede"`; forbidden for all other actions. '
            "SAGE creates a `supersedes` edge from the new version to "
            "this document atomically with the lifecycle transition. "
            "The successor document must already exist and be active; "
            "this operation does not create it."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, run all validators and compute "
            "the would-be projection of the post-transition state, but "
            "do NOT persist. The response carries the would-be "
            "`document`, `dry_run=true`, and a `changes` block (T-0163) "
            "with a single `lifecycle_status` entry when the action "
            "changes state; for `supersede`, the would-be `created_edge` "
            "is also populated with the nil-UUID sentinel id "
            "`00000000-0000-0000-0000-000000000000` so a caller that "
            "mistakes it for a real edge id fails loudly on lookup "
            "(not duplicated in `changes`). The per-document lock is "
            "still acquired so the preview is consistent with "
            "concurrent mutations."
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
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`; in that "
            "case no state was written and `document` carries the "
            "would-be projection of the post-transition state without "
            "the persisted `updated_at` advance."
        ),
    )
    changes: list[FieldChange] | None = Field(
        default=None,
        description=(
            "T-0163: Field-level deltas the transition would persist on a "
            "real run. Populated only when `dry_run=true`; null on "
            "real-run responses. For lifecycle transitions, contains a "
            "single entry for `lifecycle_status` when the action changes "
            "state. The would-be `supersedes` edge surfaces in "
            "`created_edge` and is NOT duplicated in `changes` (edge "
            "mutations are a separate concept from field-level deltas)."
        ),
    )
    created_edge: Edge | None = Field(
        default=None,
        description=(
            "T-0152: Populated only when `action=supersede`. On a real "
            "run, this is the `supersedes` edge that was created "
            "atomically with the lifecycle flip. On a dry-run, this is "
            "the would-be edge with the nil-UUID sentinel `id` "
            "`00000000-0000-0000-0000-000000000000`; the real `id` is "
            "non-deterministic and assigned at commit time."
        ),
    )


class BulkLifecycleItem(BaseModel):
    """One lifecycle transition request inside a bulk batch.

    Mirrors ``SetLifecycleRequest`` plus the ``document_id`` carried in
    the request body (since the bulk endpoint does not address documents
    via the URL path).
    """

    document_id: DocumentIdStr = Field(description="Target document id for this item.")
    action: str = Field(
        description=(
            "Lifecycle transition action. Vault-config-defined; same "
            "shape as `SetLifecycleRequest.action`."
        )
    )
    successor_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document id of the replacement version. Required when "
            '`action="supersede"`; forbidden for all other actions. Same '
            "shape and semantics as `SetLifecycleRequest.successor_id`."
        ),
    )


class BulkLifecycleRequest(BaseModel):
    """Request body for the bulk lifecycle endpoint.

    Carries an ordered list of per-item lifecycle requests. The list may
    be empty; the response then has an empty ``results`` array.
    """

    items: list[BulkLifecycleItem] = Field(
        description=(
            "Items processed in order. Each item runs in its own "
            "per-document lock and its own SQLite transaction; the batch "
            "as a whole is NOT atomic (CAS-ADR-029). A bad item does not "
            "roll back earlier-or-later successful items."
        ),
    )
    response_mode: ResponseMode | None = Field(
        default=None,
        description=(
            'Per-item payload depth (T-0153). "full" returns each '
            "success item's complete `document` body (including the "
            'potentially-large `semantic_abstract`); "light" strips the '
            "per-item `document` field entirely, returning only identity "
            "+ status + warnings + error so the response stays inside "
            "the MCP inline-output budget. Failure entries carry the "
            "full structured error envelope regardless of mode. When "
            'unset, batches with more than 5 items default to "light", '
            'smaller batches default to "full".'
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, every item runs in dry-run "
            "mode — validators execute, the would-be projection of the "
            "post-state is computed, and each per-item result carries a "
            "`changes` block (T-0163) with a single `lifecycle_status` "
            "entry on state-changing transitions (preserved under "
            "`response_mode=light`). No persistence occurs. Per-item "
            "override is not supported. **Limitation:** each item's "
            "dry-run is evaluated against the committed state at batch "
            "start; no item's would-be effects are visible to "
            "subsequent items. For full preview accuracy under "
            "sequential dependencies (e.g., item N supersedes a "
            "document and item N+1 tries to mutate it), dry-run each "
            "item separately."
        ),
    )


class BulkLifecycleItemResult(BaseModel):
    """Outcome record for a single item inside a bulk lifecycle response."""

    document_id: DocumentIdStr = Field(
        description="The target document id from the corresponding request item."
    )
    status: Literal["success", "error"] = Field(
        description=(
            "`success` if the per-item transition committed; `error` if "
            "the item raised a SAGEError and the batch continued with "
            "the next item."
        )
    )
    document: Document | None = Field(
        default=None,
        description=(
            "The updated document record when `status=success`. Absent on "
            "error entries. Also absent on success entries when the "
            "request's `response_mode=light` (T-0153)."
        ),
    )
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Same advisory shape as `SetLifecycleResponse.warnings`; "
            "present on success entries when the underlying transition "
            "emits warnings."
        ),
    )
    error: dict | None = Field(
        default=None,
        description=(
            "Error envelope when `status=error`. Shape matches the MCP "
            "`error_response` envelope: `{error: <code>, message: <text>, "
            "detail: <dict>}` where `detail` is present iff the "
            "underlying SAGEError carries one."
        ),
    )
    changes: list[FieldChange] | None = Field(
        default=None,
        description=(
            "T-0163: Field-level deltas the per-item transition would "
            "persist on a real run. Populated only when the envelope's "
            "`dry_run=true` and the per-item transition succeeds; null "
            "on real-run responses, error entries, and no-op transitions. "
            "Preserved under `response_mode=light` (it's small and is the "
            "most useful summary for dry-run callers)."
        ),
    )


class BulkLifecycleResponse(BaseModel):
    """Response body for the bulk lifecycle endpoint.

    Carries per-item outcomes plus aggregate counts. Aggregate counts are
    redundant with iterating ``results`` and exist for caller ergonomics.
    """

    results: list[BulkLifecycleItemResult] = Field(
        description="Per-item outcomes in request order."
    )
    success_count: int = Field(
        ge=0,
        description="Number of items with `status=success`.",
    )
    error_count: int = Field(
        ge=0,
        description="Number of items with `status=error`.",
    )
    total: int = Field(
        ge=0,
        description="Total items processed; equals `len(results)`.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`. Every "
            "per-item result reflects the dry-run path: success items "
            "carry the would-be projection of the post-transition "
            "document (subject to `response_mode`) and a `changes` block "
            "(T-0163) enumerating field-level deltas, and no state was "
            "written."
        ),
    )


class TagsPatch(BaseModel):
    """Patch operations on a document's tag set, used by update_metadata.

    Strict-conflict semantics enforced at the service layer (add of an
    already-present tag and remove of an absent tag both 400). The model
    validators here enforce shape-level invariants that don't require
    knowing the stored state: at least one non-empty operation must be
    present, add and remove must be disjoint, and neither list may
    contain internal duplicates.
    """

    model_config = {"extra": "forbid"}

    add: list[str] | None = Field(
        default=None,
        description=("Tags to add. None may already be present on the document."),
    )
    remove: list[str] | None = Field(
        default=None,
        description=("Tags to remove. All must currently be present on the document."),
    )

    @model_validator(mode="after")
    def _validate(self) -> "TagsPatch":
        if not self.add and not self.remove:
            raise ValueError(
                "tags patch carries no actionable operation; supply non-empty 'add' and/or 'remove'"
            )
        if self.add is not None and len(self.add) != len(set(self.add)):
            seen: set[str] = set()
            dups = [t for t in self.add if t in seen or seen.add(t)]  # type: ignore[func-returns-value]
            raise ValueError(f"tags.add contains duplicates: {sorted(set(dups))!r}")
        if self.remove is not None and len(self.remove) != len(set(self.remove)):
            seen2: set[str] = set()
            dups2 = [t for t in self.remove if t in seen2 or seen2.add(t)]  # type: ignore[func-returns-value]
            raise ValueError(f"tags.remove contains duplicates: {sorted(set(dups2))!r}")
        if self.add and self.remove:
            overlap = set(self.add) & set(self.remove)
            if overlap:
                raise ValueError(
                    f"tags.add and tags.remove must be disjoint; overlap: {sorted(overlap)!r}"
                )
        return self


class Tier3Patch(BaseModel):
    """Patch operations on a document's tier3_metadata dict.

    `set` keys overwrite existing values without error -- the verb is
    literal (assert this post-state value). `unset` keys must currently
    be present (strict-conflict at the service layer). `set` and `unset`
    keys must be disjoint. The merged result is validated against the
    resolved doc_type's metadata_schema after the patch is applied
    in memory.
    """

    model_config = {"extra": "forbid"}

    set: dict | None = Field(
        default=None,
        description=("Key-value pairs to set. Overwrites existing keys."),
    )
    unset: list[str] | None = Field(
        default=None,
        description=("Keys to remove. All must currently be present."),
    )

    @model_validator(mode="after")
    def _validate(self) -> "Tier3Patch":
        if not self.set and not self.unset:
            raise ValueError(
                "tier3_metadata patch carries no actionable operation; "
                "supply non-empty 'set' and/or 'unset'"
            )
        if self.unset is not None and len(self.unset) != len(set(self.unset)):
            seen: set[str] = set()
            dups = [k for k in self.unset if k in seen or seen.add(k)]  # type: ignore[func-returns-value]
            raise ValueError(f"tier3_metadata.unset contains duplicates: {sorted(set(dups))!r}")
        if self.set and self.unset:
            overlap = set(self.set) & set(self.unset)
            if overlap:
                raise ValueError(
                    f"tier3_metadata.set and unset must be disjoint; overlap: {sorted(overlap)!r}"
                )
        return self


class UpdateMetadataRequest(BaseModel):
    """Partial update of mutable document metadata.

    Only fields present in the request are modified. Scalar fields use
    set-or-omit semantics. ``tags`` and ``tier3_metadata`` take ops
    objects (``TagsPatch`` / ``Tier3Patch``); the bare-list / bare-dict
    forms are no longer accepted. See CAS-ADR-028 for the ingest-vs-update
    shape asymmetry rationale.
    """

    model_config = {"extra": "forbid"}

    title: str | None = Field(
        default=None, description="New human-readable title; omit to leave unchanged."
    )
    version_label: str | None = Field(
        default=None, description="New caller-supplied version label; omit to leave unchanged."
    )
    project: str | None = Field(
        default=None, description="New project scope for the document; omit to leave unchanged."
    )
    tags: TagsPatch | None = Field(
        default=None,
        description=(
            "Patch operations on the tag set: {add?: list[str], remove?: list[str]}. "
            "At least one key required. Strict-conflict on add-present / "
            "remove-absent. See TagsPatch."
        ),
    )
    doc_type: str | None = Field(
        default=None,
        description="Must be a valid value in the vault's document_types configuration.",
    )
    authority_scope: str | None = Field(
        default=None,
        description=(
            "New authority scope (read-classification scope used by "
            "access-control filtering); omit to leave unchanged."
        ),
    )
    document_date: DocumentDateStr = Field(
        default=None,
        description=(
            "Document calendar date (YYYY-MM-DD). Editable to correct "
            "fallback-derived dates that misattributed across UTC midnight."
        ),
    )
    tier3_metadata: Tier3Patch | None = Field(
        default=None,
        description=(
            "Patch operations on tier3_metadata: {set?: dict, unset?: list[str]}. "
            "At least one key required. `set` overwrites existing keys "
            "(verb is literal); `unset` keys must currently be present "
            "(else 400 tier3_unset_conflict). The merged result is validated "
            "against the resolved doc_type's metadata_schema. See Tier3Patch."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, run all validators and compute "
            "the would-be projection of the post-patch state, but do NOT "
            "persist. The response carries the would-be `document`, "
            "`dry_run=true`, and a `changes` block (T-0163) enumerating "
            "field-level deltas (scalar fields by bare name; tier3 keys "
            "as dotted paths; tags as full before/after lists); no "
            "chunk-store sync, no `metadata_confirmed` flip, no "
            "`updated_at` advance. The per-document lock is still "
            "acquired so the preview is consistent with concurrent "
            "mutations."
        ),
    )


class BulkMetadataItem(BaseModel):
    """One metadata patch request inside a bulk batch.

    Mirrors `UpdateMetadataRequest` plus the `document_id` carried in the
    request body (since the bulk endpoint does not address documents via
    the URL path).
    """

    model_config = {"extra": "forbid"}

    document_id: DocumentIdStr = Field(description="Target document id for this item.")
    title: str | None = Field(
        default=None, description="New human-readable title; omit to leave unchanged."
    )
    version_label: str | None = Field(
        default=None,
        description="New caller-supplied version label; omit to leave unchanged.",
    )
    project: str | None = Field(
        default=None,
        description="New project scope for the document; omit to leave unchanged.",
    )
    tags: TagsPatch | None = Field(
        default=None,
        description=(
            "Patch operations on the tag set: {add?: list[str], remove?: list[str]}. "
            "Same semantics as `UpdateMetadataRequest.tags`."
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
        description="Document calendar date (YYYY-MM-DD).",
    )
    tier3_metadata: Tier3Patch | None = Field(
        default=None,
        description=(
            "Patch operations on tier3_metadata: {set?: dict, unset?: list[str]}. "
            "Same semantics as `UpdateMetadataRequest.tier3_metadata`."
        ),
    )


class BulkMetadataRequest(BaseModel):
    """Request body for the bulk metadata endpoint.

    Carries an ordered list of per-item metadata-patch requests. The list
    may be empty; the response then has an empty `results` array.
    """

    items: list[BulkMetadataItem] = Field(
        description=(
            "Items processed in order. Each item runs in its own "
            "per-document lock and its own SQLite transaction; the batch "
            "as a whole is NOT atomic (CAS-ADR-029). A bad item does not "
            "roll back earlier-or-later successful items."
        ),
    )
    response_mode: ResponseMode | None = Field(
        default=None,
        description=(
            'Per-item payload depth (T-0153). "full" returns each '
            "success item's complete `document` body (including the "
            'potentially-large `semantic_abstract`); "light" strips the '
            "per-item `document` field entirely, returning only identity "
            "+ status + warnings + error so the response stays inside "
            "the MCP inline-output budget. Failure entries carry the "
            "full structured error envelope regardless of mode. When "
            'unset, batches with more than 5 items default to "light", '
            'smaller batches default to "full".'
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, every item runs in dry-run "
            "mode — validators execute, the would-be projection of the "
            "post-state is computed, and each per-item result carries a "
            "`changes` block (T-0163) enumerating field-level deltas "
            "(preserved under `response_mode=light`). No persistence "
            "occurs. Per-item override is not supported. "
            "**Limitation:** each item's dry-run is evaluated against "
            "the committed state at batch start; no item's would-be "
            "effects are visible to subsequent items. For full preview "
            "accuracy under sequential dependencies (e.g., item N adds "
            "tag X and item N+1 tries to add the same tag), dry-run "
            "each item separately."
        ),
    )


class BulkMetadataItemResult(BaseModel):
    """Outcome record for a single item inside a bulk metadata response."""

    document_id: DocumentIdStr = Field(
        description="The target document id from the corresponding request item."
    )
    status: Literal["success", "error"] = Field(
        description=(
            "`success` if the per-item patch committed; `error` if the "
            "item raised a SAGEError and the batch continued with the "
            "next item."
        )
    )
    document: Document | None = Field(
        default=None,
        description=(
            "The updated document record when `status=success`. Absent on "
            "error entries. Also absent on success entries when the "
            "request's `response_mode=light` (T-0153)."
        ),
    )
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Advisory messages; reserved for parity with `BulkLifecycleItemResult`. "
            "Not currently emitted by `update_metadata`."
        ),
    )
    error: dict | None = Field(
        default=None,
        description=(
            "Error envelope when `status=error`. Shape matches the MCP "
            "`error_response` envelope: `{error: <code>, message: <text>, "
            "detail: <dict>}` where `detail` is present iff the underlying "
            "SAGEError carries one."
        ),
    )
    changes: list[FieldChange] | None = Field(
        default=None,
        description=(
            "T-0163: Field-level deltas the per-item patch would persist "
            "on a real run. Populated only when the envelope's "
            "`dry_run=true` and the per-item patch succeeds; null on "
            "real-run responses, error entries, and patches that touch "
            "no caller-supplied fields. Tier3 changes enumerate per-key "
            "with dotted paths (e.g., `tier3_metadata.severity`); tags "
            "carry the full ordered before/after lists. Preserved under "
            "`response_mode=light`."
        ),
    )


class BulkMetadataResponse(BaseModel):
    """Response body for the bulk metadata endpoint.

    Carries per-item outcomes plus aggregate counts. Aggregate counts are
    redundant with iterating `results` and exist for caller ergonomics.
    """

    results: list[BulkMetadataItemResult] = Field(description="Per-item outcomes in request order.")
    success_count: int = Field(ge=0, description="Number of items with `status=success`.")
    error_count: int = Field(ge=0, description="Number of items with `status=error`.")
    total: int = Field(ge=0, description="Total items processed; equals `len(results)`.")
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`. Every "
            "per-item result reflects the dry-run path: success items "
            "carry the would-be projection of the post-patch document "
            "(subject to `response_mode`) and a `changes` block (T-0163) "
            "enumerating field-level deltas, and no state was written."
        ),
    )


class UpdateMetadataResponse(BaseModel):
    """wrapper for single-item `update_metadata`.

    Promotes the bare-Document return so the dry-run flag has a home.
    The `document` field is the post-patch state — persisted on a real
    run, computed in memory on a dry-run (the "would-be projection").
    """

    document: Document = Field(
        description=(
            "The updated document record. On a real run, this is what "
            "was persisted (with `updated_at` advanced and "
            "`metadata_confirmed=true`). On a dry-run, this is the "
            "would-be projection of the post-patch state without those "
            "side effects."
        )
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`; in that "
            "case no state was written and `document` carries the "
            "would-be projection of the post-patch state."
        ),
    )
    changes: list[FieldChange] | None = Field(
        default=None,
        description=(
            "T-0163: Field-level deltas the patch would persist on a "
            "real run. Populated only when `dry_run=true`; null on "
            "real-run responses and on dry-runs that touch no "
            "caller-supplied fields. Scalar field changes use the bare "
            "field name as `path`; tier3 changes enumerate per-key with "
            "dotted paths (e.g., `tier3_metadata.severity`); tags carry "
            "the full ordered before/after lists in `before`/`after`. "
            "Entries are sorted by `path` for determinism."
        ),
    )


class RegisterUserRequest(BaseModel):
    display_name: str = Field(description="Human-readable name for the user or agent.")
    user_type: UserType = Field(description="Actor type for provenance and access control.")


class SetEditorsRequest(BaseModel):
    """Replace the editor list on a document."""

    user_ids: list[UserIdStr] = Field(
        description=("User IDs to set as editors. An empty array restores default-open access.")
    )


class EditorList(BaseModel):
    """Editor membership for a document."""

    document_id: DocumentIdStr = Field(
        description="Id of the document whose editor list is being reported."
    )
    editors: list[User] = Field(
        description="Current editors. Empty array means default-open access."
    )


class IngestResponse(BaseModel):
    """Returned by `POST /sage_vaults/{vault_id}/documents`.

    The endpoint is synchronous and returns after the three-stage
    ingestion pipeline completes.

    The REST endpoint is synchronous: `pipeline_status` is at a terminal
    value (`abstraction_complete`, `abstraction_skipped`, or `failed`)
    by the time this response is returned. The MCP `ingest_document` wrapper
    inverts this and returns early with a non-terminal status; that
    behavior is MCP-specific and does not affect the REST surface.
    """

    document: Document = Field(description="The ingested document record.")
    pipeline_status: PipelineStatus = Field(
        description="Terminal pipeline status reached during the synchronous ingest."
    )


class OpenDocumentResponse(BaseModel):
    """Returned after dispatching a document's source file to the OS opener."""

    opened: Literal[True] = Field(
        description="Discriminator confirming the OS opener was dispatched; always true."
    )
    path: str = Field(description="Absolute filesystem path that was dispatched to the OS opener.")


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
    rationale_kind: RationaleKind | None = Field(
        default=None,
        description=(
            "Optional explicit discriminator (CAS-ADR-019 / T-0080). "
            "When omitted or null, the service derives the value from "
            "the rationale text prefix and falls back to `manual` for "
            "unrecognized or absent rationale. Callers should pass this "
            "only when they have stronger provenance information than "
            "the prefix-derivation rule."
        ),
    )
    synced_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "The source-chain version (document id) the content was "
            "copied or derived from at the moment this edge was "
            "asserted. Semantically meaningful on `sync_target` (Tier 1, "
            "populated automatically at re-ingestion when the Tier-1 "
            "inference subsystem ships) and `derived_from` (Tier 3, "
            "agent-supplied via create_edge). Distinct from "
            "`source_valid_from_version`, which records chain-scoped "
            "edge visibility per CAS-ADR-017 — the two must not be "
            "conflated. Unset = explicit null; never inferred from chain "
            "anchors. (T-0110 schema; T-0111 typed)"
        ),
    )
    synced_from_content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "The source document's `source_content_hash` captured at "
            "the moment this edge was asserted. Optional companion to "
            "`synced_from_version`; recommended on derivations because "
            "version labels are reused and can drift from content "
            "(in-place edits). Must match `^sha256:[0-9a-f]{64}$`; "
            "unset = explicit null. (T-0110 schema; T-0111 typed)"
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, run all validators (including "
            "the T-0079 natural-key pre-check) and compute the would-be "
            "projection of the edge, but do NOT persist. The response "
            "carries the would-be `edge` with the nil-UUID sentinel id "
            "`00000000-0000-0000-0000-000000000000` (or the existing "
            "edge id on a natural-key collision, with `created=false`) "
            "and `dry_run=true`. Note: link is an edge mutation, not a "
            "document field mutation, so the change surface is the "
            "existing `edge` field rather than a separate `changes` "
            "block (T-0163). The process-wide `_link_lock` is still "
            "acquired so the preview is consistent with concurrent link "
            "writes."
        ),
    )


class LinkResponse(BaseModel):
    """wrapper for `create_edge` return.

    Promotes the previously-hand-constructed `{edge, created,...}`
    dict to a real schema so the dry-run flag has a home.
    """

    edge: Edge = Field(
        description=(
            "The edge record. On a real run, this is the persisted edge "
            "(with `created=true`) or the existing edge that was matched "
            "by the natural-key idempotency check (with `created=false`). "
            "On a dry-run, this is the would-be edge with the nil-UUID "
            "sentinel id `00000000-0000-0000-0000-000000000000` (or the "
            "existing edge id on a natural-key hit)."
        )
    )
    created: bool = Field(
        description=(
            "True iff a new edge would be (or was) created. False on "
            "the natural-key idempotency path where an edge already "
            "exists for `(source, target, edge_type)`."
        )
    )
    existing_rationale: str | None = Field(
        default=None,
        description=(
            "When `created=false`, the rationale stored on the existing "
            "edge that satisfied the idempotency check. Null otherwise."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`; in that "
            "case no state was written and `edge` carries the would-be "
            "projection of the edge that would be persisted (nil-UUID "
            "sentinel id, or the existing edge id on a natural-key hit). "
            "Note: link is an edge mutation, not a document field "
            "mutation, so the change surface is the existing `edge` "
            "field rather than a separate `changes` block (T-0163)."
        ),
    )


class BulkLinkItem(BaseModel):
    """One edge-creation request inside a bulk batch.

    Mirrors ``LinkRequest`` field-for-field except for the envelope-level
    ``dry_run`` (which lives on ``BulkLinkRequest`` and is propagated to
    each item; per-item override is not supported per CAS-ADR-029).
    """

    model_config = {"extra": "forbid"}

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
            "Source-chain anchor (same semantics as `LinkRequest.source_valid_from_version`)."
        ),
    )
    target_valid_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Target-chain anchor (same semantics as `LinkRequest.target_valid_from_version`)."
        ),
    )
    retracted_edge_id: EdgeIdStr | None = Field(
        default=None,
        description="Required for `retracts` edges; null for every other edge type.",
    )
    notes: str | None = Field(default=None, description="Optional annotation.")
    rationale: str | None = Field(
        default=None,
        description="Decision rationale for creating this relationship.",
    )
    rationale_kind: RationaleKind | None = Field(
        default=None,
        description=(
            "Optional explicit discriminator (CAS-ADR-019 / T-0080). Same "
            "semantics as `LinkRequest.rationale_kind`."
        ),
    )
    synced_from_version: DocumentIdStr | None = Field(
        default=None,
        description=(
            "T-0110 / T-0111 provenance field. Same semantics as `LinkRequest.synced_from_version`."
        ),
    )
    synced_from_content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "T-0110 / T-0111 provenance field. Same semantics as "
            "`LinkRequest.synced_from_content_hash`."
        ),
    )


class BulkLinkRequest(BaseModel):
    """Request body for the bulk-link endpoint.

    Carries an ordered list of per-item edge-creation requests. The list
    may be empty; the response then has an empty ``results`` array.
    """

    items: list[BulkLinkItem] = Field(
        description=(
            "Items processed in order. Each item runs under the process-"
            "wide `_link_lock` and a per-item SQLite transaction; the "
            "batch as a whole is NOT atomic (CAS-ADR-029). A bad item "
            "does not roll back earlier-or-later successful items."
        ),
    )
    response_mode: ResponseMode | None = Field(
        default=None,
        description=(
            'Per-item payload depth (T-0153 / T-0158). "full" returns '
            "each success item's complete `edge` body (the persisted "
            'edge, or the would-be edge under dry-run); "light" strips '
            "the per-item `edge` field entirely, returning only "
            "identity + status + created + existing_rationale + error so "
            "the response stays inside the MCP inline-output budget. "
            "Failure entries carry the full structured error envelope "
            "regardless of mode. When unset, batches with more than 5 "
            'items default to "light", smaller batches default to "full".'
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, every item runs in dry-run "
            "mode — validators execute (including the T-0079 natural-key "
            "pre-check), the would-be projection of the edge is computed, "
            "and each per-item result carries the would-be `edge` with "
            "the nil-UUID sentinel id (or the existing edge id on a "
            "natural-key hit, with `created=false`). No persistence "
            "occurs. Note: link is an edge mutation, not a document "
            "field mutation, so the change surface is the per-item "
            "`edge` field (subject to `response_mode`) rather than a "
            "separate `changes` block (T-0163). Envelope-level only; "
            "per-item override is not supported. **Limitation:** each "
            "item's dry-run is evaluated against the committed state at "
            "batch start; no item's would-be effects are visible to "
            "subsequent items. For full preview accuracy under "
            "sequential dependencies (e.g., item N creates an edge and "
            "item N+1 references it via retracted_edge_id), dry-run "
            "each item separately."
        ),
    )


class BulkLinkItemResult(BaseModel):
    """Outcome record for a single item inside a bulk-link response."""

    source_id: DocumentIdStr = Field(
        description="Echoed from the request item for caller correlation."
    )
    target_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Echoed from the request item for caller correlation. Null on "
            "`retracts` items (where the request also carries null)."
        ),
    )
    edge_type: EdgeType = Field(description="Echoed from the request item for caller correlation.")
    status: Literal["success", "error"] = Field(
        description=(
            "`success` if the per-item link committed (real-run) or would "
            "have committed (dry-run); `error` if the item raised a "
            "SAGEError and the batch continued with the next item."
        )
    )
    edge: Edge | None = Field(
        default=None,
        description=(
            "The edge record when `status=success`. Absent on error "
            "entries. Also absent on success entries when the request's "
            "`response_mode=light` (T-0153). On a real run this is the "
            "persisted edge (with `created=true`) or the existing edge "
            "that satisfied the natural-key idempotency check (with "
            "`created=false`). On a dry-run this is the would-be edge "
            "with the nil-UUID sentinel id (or the existing edge id on a "
            "natural-key hit)."
        ),
    )
    created: bool | None = Field(
        default=None,
        description=(
            "T-0079 idempotency flag. True iff a new edge was (or would "
            "be) created for this item; false on the natural-key "
            "idempotency path where an edge already exists for "
            "`(source, target, edge_type)`. Null on error entries."
        ),
    )
    existing_rationale: str | None = Field(
        default=None,
        description=(
            "When `created=false`, the rationale stored on the existing "
            "edge that satisfied the idempotency check. Null on "
            "create-path successes and on error entries. Preserved under "
            "`response_mode=light` so callers can distinguish the no-op "
            "outcome from a fresh insert without the full `edge` body."
        ),
    )
    error: dict | None = Field(
        default=None,
        description=(
            "Error envelope when `status=error`. Shape matches the MCP "
            "`error_response` envelope: `{error: <code>, message: <text>, "
            "detail: <dict>}` where `detail` is present iff the "
            "underlying SAGEError carries one."
        ),
    )


class BulkLinkResponse(BaseModel):
    """Response body for the bulk-link endpoint.

    Carries per-item outcomes plus aggregate counts. Aggregate counts are
    redundant with iterating ``results`` and exist for caller ergonomics.
    """

    results: list[BulkLinkItemResult] = Field(description="Per-item outcomes in request order.")
    success_count: int = Field(
        ge=0,
        description="Number of items with `status=success`.",
    )
    error_count: int = Field(
        ge=0,
        description="Number of items with `status=error`.",
    )
    total: int = Field(
        ge=0,
        description="Total items processed; equals `len(results)`.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`. Every "
            "per-item result reflects the dry-run path: success items "
            "carry the would-be edge (subject to `response_mode`) with "
            "the nil-UUID sentinel id on the create path or the existing "
            "edge id on a natural-key hit, and no state was written."
        ),
    )


class UnlinkResponse(BaseModel):
    """Returned after deleting (or previewing the deletion of) a production edge."""

    deleted: bool = Field(
        description=("True on a successful real-run deletion. False on a dry-run preview (T-0152).")
    )
    edge_id: EdgeIdStr = Field(description="Id of the edge that was (or would be) deleted.")
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the unlink call set `dry_run=true`; in "
            "that case no state was written and `preview_edge` carries "
            "the would-be projection of the edge that would be deleted. "
            "Note: unlink is an edge mutation, not a document field "
            "mutation, so the change surface is the existing "
            "`preview_edge` field rather than a separate `changes` "
            "block (T-0163)."
        ),
    )
    preview_edge: Edge | None = Field(
        default=None,
        description=(
            "T-0152: On a dry-run, the existing edge that would be deleted. Null on a real-run."
        ),
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

    @classmethod
    def from_traversal(
        cls,
        document: "DocumentSummary",
        edge: "Edge",
        depth: int,
        edge_counts: dict[str, int],
    ) -> "TraversalNode":
        """Build a TraversalNode from its component parts.

        Single owner of the (DocumentSummary, Edge, depth, edge_counts) →
        TraversalNode projection per the *CAS Projection-Point Audit
        Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        `test_from_traversal_populates_every_traversal_node_field` in
        `tests/sage/test_graph_ops.py` fails closed if a field is added
        to TraversalNode but not wired through this factory.
        """
        return cls(
            document=document,
            edge=edge,
            depth=depth,
            edge_counts=edge_counts,
        )


class TraverseResponse(BaseModel):
    start_id: DocumentIdStr = Field(description="The document ID traversal started from.")
    nodes: list[TraversalNode] = Field(
        description="Traversal nodes ordered by depth, then by edge insertion order."
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
        ge=1,
        description=(
            "Maximum number of chain entries to return after offset is "
            "applied. Null (default) returns the entire chain. Pagination "
            "is caller-side slicing performed on the full chain returned "
            "by the graph walk; the walk itself is unbounded."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of chain entries to skip from the start of the "
            "ordered chain before applying `limit`. Used together with "
            "`limit` to page through long chains."
        ),
    )


class ChainEntry(BaseModel):
    id: DocumentIdStr = Field(description="Document ID.")
    title: str = Field(description="Human-readable title of this chain entry's document.")
    version_label: str | None = Field(
        default=None,
        description="Caller-supplied version label for this entry, when set; null otherwise.",
    )
    lifecycle_status: str = Field(
        description="Lifecycle state of this entry's document at the time the chain was walked."
    )
    document_date: DocumentDateStr = Field(
        default=None,
        description="Authoritative content date (YYYY-MM-DD) if available.",
    )
    position: int = Field(
        description="Zero-based ordinal position in the chain (0 = tail, length-1 = head)."
    )

    @classmethod
    def from_chain_row(cls, row: dict, position: int) -> "ChainEntry":
        """Build a ChainEntry from a chain-walk CTE row dict and position.

        Single owner of the chain-walk row dict -> ChainEntry projection per the
        *CAS Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_from_chain_row_populates_every_chain_entry_field`` in
        ``tests/sage/test_graph_ops.py`` fails closed if a field is added to
        ChainEntry but not wired through this factory.

        The row dict is the per-document shape produced by
        ``GraphStore.chain_walk`` (keys: ``doc_id``, ``title``, ``version_label``,
        ``lifecycle_status``, ``document_date``). ``position`` is supplied by
        the caller's chain-ordering pass.
        """
        return cls(
            id=row["doc_id"],
            title=row["title"],
            version_label=row["version_label"],
            lifecycle_status=row["lifecycle_status"],
            document_date=row["document_date"],
            position=position,
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
    length: int = Field(description="Total number of documents in the chain.")
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
            'Required condition (e.g., "exists and lifecycle_status in [active, completed]").'
        )
    )
    actual: str = Field(description='Actual state found (e.g., "active", "not found").')
    satisfied: bool = Field(
        description="True when the actual state meets the required condition for this target."
    )


class PreconditionResult(BaseModel):
    function_id: FunctionIdStr = Field(
        description="Identifier of the workflow function whose preconditions were checked."
    )
    satisfied: bool = Field(description="True if all dependencies are satisfied.")
    checks: list[PreconditionCheck] = Field(
        description="Per-target check results making up this precondition evaluation."
    )


# ---------------------------------------------------------------------------
# Discover (retrieval) models
# ---------------------------------------------------------------------------


class RetrievalFilters(BaseModel):
    """Metadata filters applied before retrieval.

    Used when scope is "filtered", but also applicable as additional
    constraints with other scopes.
    """

    # extra="forbid": unknown filter keys raise rather than silently
    # drop. The pre-default ("ignore") turned a typo like
    # ``{"tickett_id": ""}`` into a no-op match-everything filter; the
    # translator in sage.api.errors now converts the extra_forbidden
    # ValidationError into a typed UnknownFilterKeyError envelope.
    model_config = ConfigDict(extra="forbid")

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
        description='Restrict to a specific set of documents. Used with scope "specific".',
    )
    pipeline_status: str | None = Field(
        default=None,
        description=(
            'Filter by pipeline status (e.g., "failed", '
            '"abstraction_complete"). Overrides the default exclusion '
            "of failed-pipeline documents."
        ),
    )
    tier3_metadata: dict | None = Field(
        default=None,
        description=(
            "Tier 3 (per-doc_type typed metadata) post-filter. Each "
            "key/value pair in the dict is matched against the document's "
            "`tier3_metadata` dict via exact equality. A value of null "
            "matches documents whose stored field is either null or absent "
            "from the tier3_metadata dict. All pairs AND together; an empty "
            "dict is treated as no filter."
        ),
    )
    # Edge-only filter keys. Valid only when the DiscoverRequest
    # targets edges; document-targeting requests that set these are rejected
    # at the DiscoverRequest model_validator (mode_parameter_mismatch).
    source_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Edge-only filter (T-0157). Filter edges by source document "
            'id. Valid only when target="edges"; document-target '
            "requests that set this are rejected via "
            "mode_parameter_mismatch."
        ),
    )
    target_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Edge-only filter (T-0157). Filter edges by target document "
            'id. Valid only when target="edges". Note: retracts-type '
            "edges have target_id=NULL and are not selected by this "
            "filter."
        ),
    )
    edge_type: EdgeType | None = Field(
        default=None,
        description=(
            "Edge-only filter (T-0157). Filter edges by edge_type (e.g., "
            '"references", "depends_on"). Valid only when target="edges". '
            "Typed against the SAGE EdgeType enum so a typo like "
            '"refrences" is rejected at validation time rather than '
            "silently returning zero rows."
        ),
    )


# Document-only filter keys that must NOT be set when target="edges".
_DOC_ONLY_FILTER_KEYS: tuple[str, ...] = (
    "doc_type",
    "project",
    "lifecycle_status",
    "tags",
    "document_ids",
    "pipeline_status",
    "tier3_metadata",
)

# Edge-only filter keys that must NOT be set when target="documents".
_EDGE_ONLY_FILTER_KEYS: tuple[str, ...] = (
    "source_id",
    "target_id",
    "edge_type",
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
        description=(
            "Retrieval scope that narrows the candidate set before "
            'relevance ranking. Default "all" applies no scope filter.'
        ),
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
            '"Section 3 > Definitions > Normalization"). Required for '
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
            "When true, include the `semantic_abstract` field on each "
            "returned chunk's document record. Off by default because "
            "abstracts are large strings and inflate the response "
            "payload; turn on when the caller needs to inspect or "
            "display abstracts. Applies to semantic and keyword modes."
        ),
    )
    min_relevance: float | None = Field(
        default=None,
        description=(
            "Minimum relevance score (0.0-1.0) required for a chunk to "
            "appear in results. Null (default) applies no threshold. "
            "Used to suppress weak matches when the caller wants tight "
            "precision; the filter is applied after ranking and before "
            "limit is applied, so weak-match suppression may reduce the "
            "returned result count below the requested limit."
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
    target: RetrievalTarget = Field(
        default=RetrievalTarget.DOCUMENTS,
        description=(
            "Selects whether the query enumerates documents (default) or "
            'edges. "edges" is valid only with mode=catalog and with '
            "edge-only filter keys (source_id, target_id, edge_type); "
            "other mode/parameter combinations are rejected via "
            "mode_parameter_mismatch. (T-0157)"
        ),
    )
    response_mode: ResponseMode | None = Field(
        default=None,
        description=(
            "Canonical payload-depth selector across SAGE surfaces "
            "(T-0157, T-0158, T-0153, T-0169). Semantics by target and "
            "mode: (edges) `light` returns identity columns only "
            "(edge_id, endpoints, edge_type); `full` carries the "
            "complete envelope; default obeys a >5-results threshold "
            "rule. (documents+catalog) `light` returns a stripped "
            "DocumentSummaryLight; `full` returns the full "
            "DocumentSummary; the threshold rule does NOT apply -- "
            "default is full-equivalent. (documents+semantic/keyword) "
            "`light` suppresses chunk_content; `full` includes it. "
            "(documents+deterministic) ignored; deterministic always "
            "returns chunk content."
        ),
    )

    @model_validator(mode="after")
    def _reject_mode_parameter_mismatch(self) -> "DiscoverRequest":
        """Reject parameter/mode combinations that have no defined semantics
        . Required-but-absent cases (e.g., semantic mode without
        ``query``) are still handled at the service layer via
        ``MissingFieldError`` to preserve the existing ``missing_*`` typed
        codes. This validator only catches the inverse case: a parameter is
        set that is not valid for the chosen mode.

        Raises a ``PydanticCustomError`` carrying ``mode_parameter_mismatch``
        as the error type plus the structured detail. The translator in
        ``sage.api.errors`` reconstructs the public-facing
        ``ModeParameterMismatchError`` SAGEError from the embedded ``ctx``.
        This indirection respects the "models are a leaf layer"
        import-linter contract: sage.models cannot import from sage.api.
        """
        if self.mode != RetrievalMode.DETERMINISTIC and self.heading_path is not None:
            raise PydanticCustomError(
                "mode_parameter_mismatch",
                (
                    "Parameter 'heading_path' is not valid for mode "
                    "'{mode}'. Allowed: deterministic only."
                ),
                {
                    "mode": self.mode.value,
                    "forbidden_param": "heading_path",
                    "allowed_modes": [RetrievalMode.DETERMINISTIC.value],
                },
            )
        if self.mode == RetrievalMode.DETERMINISTIC and self.query is not None:
            raise PydanticCustomError(
                "mode_parameter_mismatch",
                (
                    "Parameter 'query' is not valid for mode 'deterministic'. "
                    "Allowed: semantic, keyword."
                ),
                {
                    "mode": self.mode.value,
                    "forbidden_param": "query",
                    "allowed_modes": [
                        RetrievalMode.SEMANTIC.value,
                        RetrievalMode.KEYWORD.value,
                    ],
                },
            )

        # Target=edges is valid only with mode=catalog.
        if self.target == RetrievalTarget.EDGES and self.mode != RetrievalMode.CATALOG:
            raise PydanticCustomError(
                "mode_parameter_mismatch",
                ("Target 'edges' is not valid for mode '{mode}'. Allowed: catalog only."),
                {
                    "mode": self.mode.value,
                    "forbidden_param": "target",
                    "allowed_modes": [RetrievalMode.CATALOG.value],
                },
            )

        # Target=edges rejects document-only filter keys.
        if self.target == RetrievalTarget.EDGES and self.filters is not None:
            for key in _DOC_ONLY_FILTER_KEYS:
                if getattr(self.filters, key) not in (None, [], {}):
                    raise PydanticCustomError(
                        "mode_parameter_mismatch",
                        (
                            "Filter key 'filters.{key}' is not valid for "
                            "target 'edges'. Allowed filter keys: "
                            "source_id, target_id, edge_type."
                        ),
                        {
                            "mode": self.mode.value,
                            "forbidden_param": f"filters.{key}",
                            "allowed_modes": [RetrievalMode.CATALOG.value],
                            "key": key,
                        },
                    )

        # Target=documents rejects edge-only filter keys.
        if self.target == RetrievalTarget.DOCUMENTS and self.filters is not None:
            for key in _EDGE_ONLY_FILTER_KEYS:
                if getattr(self.filters, key) is not None:
                    raise PydanticCustomError(
                        "mode_parameter_mismatch",
                        (
                            "Filter key 'filters.{key}' is not valid for "
                            "target 'documents'. Use ``target=\"edges\"`` "
                            "for edge enumeration. (T-0157)"
                        ),
                        {
                            "mode": self.mode.value,
                            "forbidden_param": f"filters.{key}",
                            "allowed_modes": [RetrievalMode.CATALOG.value],
                            "key": key,
                        },
                    )

        # Target=edges rejects doc-only request parameters. Only
        # explicitly non-default values are flagged so callers can leave
        # the other knobs alone without triggering this branch.
        if self.target == RetrievalTarget.EDGES:
            edge_forbidden_params: list[tuple[str, object, object]] = [
                ("query", self.query, None),
                ("document_id", self.document_id, None),
                ("heading_path", self.heading_path, None),
                ("min_relevance", self.min_relevance, None),
                ("sort_by", self.sort_by, None),
                ("sort_order", self.sort_order, None),
                ("include_abstracts", self.include_abstracts, False),
            ]
            for name, value, default in edge_forbidden_params:
                if value != default:
                    raise PydanticCustomError(
                        "mode_parameter_mismatch",
                        ("Parameter '{forbidden_param}' is not valid for target 'edges'. (T-0157)"),
                        {
                            "mode": self.mode.value,
                            "forbidden_param": name,
                            "allowed_modes": [RetrievalMode.CATALOG.value],
                        },
                    )

        return self


class DiscoverHit(BaseModel):
    """A single retrieval result. Fields populated depend on the retrieval mode."""

    document: DocumentSummary | DocumentSummaryLight = Field(
        description=(
            "Compact summary of the matching document. Returns "
            "DocumentSummaryLight (stripped) when the request set "
            '`target="documents", mode="catalog", response_mode="light"` '
            "(T-0158); DocumentSummary (full) otherwise."
        )
    )
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
            "Heading hierarchy path of the retrieved chunk (e.g., "
            '"Section 3 > Definitions > Normalization").'
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

    @classmethod
    def from_summary(
        cls,
        document: "DocumentSummary | DocumentSummaryLight",
        *,
        chunk_content: str | None = None,
        heading_path: str | None = None,
        relevance_score: float | None = None,
        matched_chunk_count: int | None = None,
    ) -> "DiscoverHit":
        """Build a DiscoverHit from a DocumentSummary plus optional chunk fields.

        Single owner of the DocumentSummary → DiscoverHit projection per the *CAS
        Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_from_summary_populates_every_discover_hit_field`` in
        ``tests/sage/test_retrieval.py`` fails closed if a field is added to
        DiscoverHit but not wired through this factory.

        ``document`` accepts either the full ``DocumentSummary`` or the
        stripped ``DocumentSummaryLight``. The light variant is
        only returned by the catalog+documents+response_mode=light path;
        every other path supplies a full ``DocumentSummary``.
        """
        return cls(
            document=document,
            chunk_content=chunk_content,
            heading_path=heading_path,
            relevance_score=relevance_score,
            matched_chunk_count=matched_chunk_count,
        )


class EdgeHit(BaseModel):
    """A single edge enumeration result.

    Returned by ``search`` when ``target="edges"``. Field
    population depends on ``response_mode``: ``light`` returns only
    identity columns (``edge_id``, ``source_id``, ``target_id``,
    ``edge_type``); ``full`` returns the complete envelope including
    anchor versions, rationale, and retraction state.

    Retraction-state fields disambiguate two perspectives:
    - ``retracted_edge_id`` is the native column on ``retracts``-type
      rows: when this row is itself a retracts edge, this field carries
      the id of the edge being disclaimed.
    - ``retracted_at`` and ``retracted_by_edge_id`` are computed via
      LEFT JOIN: when this row is a non-retracts edge that has been
      disclaimed by a later ``retracts`` edge, these fields carry the
      timestamp and id of the earliest disclaiming edge.

    Either set may be populated independently; non-retracted regular
    edges have all three retraction fields null.
    """

    edge_id: EdgeIdStr = Field(description="UUID of this edge row.")
    source_id: DocumentIdStr = Field(description="Source document id.")
    target_id: DocumentIdStr | None = Field(
        default=None,
        description=("Target document id. Null for retracts-type edges (CAS-ADR-017)."),
    )
    edge_type: EdgeType = Field(description='Edge type (e.g., "references", "retracts").')
    source_valid_from_version: str | None = Field(
        default=None,
        description="Source-chain anchor version (CAS-ADR-017). Omitted in light mode.",
    )
    target_valid_from_version: str | None = Field(
        default=None,
        description="Target-chain anchor version (CAS-ADR-017). Omitted in light mode.",
    )
    rationale: str | None = Field(
        default=None,
        description="Human or machine rationale for this edge. Omitted in light mode.",
    )
    rationale_kind: str | None = Field(
        default=None,
        description=(
            "Typed provenance discriminator (T-0080: manual, version_chain, "
            "references_mention, filename_code_match). Omitted in light mode."
        ),
    )
    retracted_edge_id: EdgeIdStr | None = Field(
        default=None,
        description=(
            "Native column. Populated only on retracts-type rows; carries "
            "the id of the edge being disclaimed by this retracts edge. "
            "Omitted in light mode."
        ),
    )
    retracted_at: datetime | None = Field(
        default=None,
        description=(
            "Computed. When this edge (a non-retracts row) has been "
            "disclaimed by a later retracts edge, carries the timestamp "
            "of the earliest disclaiming edge. Null when this edge is "
            "still live. Omitted in light mode."
        ),
    )
    retracted_by_edge_id: EdgeIdStr | None = Field(
        default=None,
        description=(
            "Computed. The edge_id of the earliest retracts edge that "
            "disclaims this row. Null when this edge is still live. "
            "Omitted in light mode."
        ),
    )


class DiscoverResponse(BaseModel):
    mode: RetrievalMode = Field(description="The retrieval mode that produced these results.")
    target: RetrievalTarget = Field(
        default=RetrievalTarget.DOCUMENTS,
        description=(
            'The result row type. "documents" (default) yields '
            'DiscoverHit rows; "edges" yields EdgeHit rows. Consumers '
            "switch on this field to know how to read `results`. "
            "(T-0157)"
        ),
    )
    results: list[DiscoverHit] | list[EdgeHit] = Field(
        description=(
            "Retrieval hits, ordered by descending relevance "
            "(semantic/keyword) or by sort_by (catalog) for documents; "
            "ordered by edge created_at DESC for edges. Row type is "
            "discriminated by `target`."
        )
    )
    total_available: int = Field(
        description=(
            "Total number of results available (before pagination). May be "
            "approximate for semantic mode."
        )
    )
    hints: dict[str, object] | None = Field(
        default=None,
        description=(
            "Optional retrieval hints surfaced to the caller. Null when no "
            "hints apply. Empty-result hints (all modes): "
            "`total_before_filtering`, plus `active_filters` and `scope` "
            "when applicable. Catalog budget hint (T-0091, fires when the "
            "serialized response exceeds the MCP inline ceiling): "
            '`reason="response_exceeds_inline_budget"`, '
            "`response_size_bytes`, `budget_bytes`, `recommended_limit` "
            "(re-page at this limit to fit inline)."
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
    document_id: DocumentIdStr = Field(description="Id of the document whose projection was read.")
    title: str = Field(description="Human-readable title of the document.")
    version_label: str | None = Field(
        default=None,
        description="Caller-supplied version label for the document, when set; null otherwise.",
    )
    lifecycle_status: str = Field(description="Current lifecycle state of the document.")
    doc_type: str | None = Field(
        default=None,
        description=(
            "Vault-domain document type from the doc_types vocabulary; null when not classified."
        ),
    )
    source_path: str = Field(description="Vault-local path to the document's source file.")
    projection_text: str | None = Field(
        default=None,
        description=(
            "Full canonical projection text reassembled from stored chunks. "
            "Populated when the read returns the projection inline. Null when "
            "the caller supplied write_to_path and the projection bytes were "
            "delivered to disk instead."
        ),
    )
    written_to: str | None = Field(
        default=None,
        description=(
            "Absolute path where SAGE wrote the projection text. Populated "
            "only when the request specified write_to_path. Equals the "
            "caller-supplied value."
        ),
    )
    content_size: int | None = Field(
        default=None,
        description=(
            "Byte count of the projection text written to disk. Populated "
            "only when the request specified write_to_path."
        ),
    )

    @classmethod
    def from_document(cls, doc: "Document", projection_text: str) -> "ReadProjectionResponse":
        """Build a ReadProjectionResponse from a Document plus projection text.

        Single owner of the Document → ReadProjectionResponse projection per the
        *CAS Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_from_document_populates_every_read_projection_response_field``
        in ``tests/sage/test_utilities.py`` fails closed if a field is added
        to ReadProjectionResponse but not wired through this factory --
        except the optional ``written_to`` / ``content_size`` delivery
        fields, which are populated by the service layer's write_to_path
        branch (UtilitiesService.read_projection), not the factory.
        """
        return cls(
            document_id=doc.id,
            title=doc.title,
            version_label=doc.version_label,
            lifecycle_status=doc.lifecycle_status,
            doc_type=doc.doc_type,
            source_path=doc.source_path,
            projection_text=projection_text,
        )


class ReadSectionResponse(BaseModel):
    document_id: DocumentIdStr = Field(description="Id of the document whose section was read.")
    title: str = Field(description="Human-readable title of the document.")
    heading_path: str = Field(description="Heading-path prefix that scoped the section read.")
    chunk_count: int = Field(description="Number of chunks matched by the heading_path prefix.")
    section_text: str = Field(description="Concatenated text of all chunks under the heading path.")

    @classmethod
    def from_document(
        cls,
        doc: "Document",
        heading_path: str,
        chunk_count: int,
        section_text: str,
    ) -> "ReadSectionResponse":
        """Build a ReadSectionResponse from a Document plus section fields.

        Single owner of the Document → ReadSectionResponse projection per the
        *CAS Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_from_document_populates_every_read_section_response_field`` in
        ``tests/sage/test_utilities.py`` fails closed if a field is added to
        ReadSectionResponse but not wired through this factory.
        """
        return cls(
            document_id=doc.id,
            title=doc.title,
            heading_path=heading_path,
            chunk_count=chunk_count,
            section_text=section_text,
        )


class ListHeadingsResponse(BaseModel):
    document_id: DocumentIdStr = Field(description="Id of the document whose headings were listed.")
    title: str = Field(description="Human-readable title of the document.")
    headings: list[str] = Field(
        description=(
            "Distinct heading paths in document order, suitable for passing to read_section."
        )
    )

    @classmethod
    def from_document(cls, doc: "Document", headings: list[str]) -> "ListHeadingsResponse":
        """Build a ListHeadingsResponse from a Document plus heading list.

        Single owner of the Document → ListHeadingsResponse projection per the
        *CAS Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_from_document_populates_every_list_headings_response_field`` in
        ``tests/sage/test_utilities.py`` fails closed if a field is added to
        ListHeadingsResponse but not wired through this factory.
        """
        return cls(
            document_id=doc.id,
            title=doc.title,
            headings=headings,
        )


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


class MigrationReportEntry(BaseModel):
    table: str = Field(
        description="SQLite table name whose schema was altered (e.g., 'documents', 'edges')."
    )
    column: str = Field(description="Column name added to the table by this migration.")


class Tier3UniquenessCollision(BaseModel):
    """One value held by multiple chain heads of the same doc_type.

    Surfaced by the migration scan when a vault's `unique_keys` declaration
    cannot be activated because the existing portfolio violates the
    constraint. Per CAS-ADR-031 §5 the substrate refuses to activate (does
    not auto-resolve, does not flag-and-pass); the operator must resolve
    the collision via renumbering, supersession, or archive-and-recreate
    before the constraint can take effect.
    """

    doc_type: str = Field(description="Document type whose unique_keys declaration was scanned.")
    field: str = Field(description="The tier3_metadata field whose values collided.")
    value: object = Field(description="The colliding value held by more than one chain head.")
    document_ids: list[DocumentIdStr] = Field(
        description=(
            "All chain-head document ids holding this value (length >= 2). "
            "Each id is the head of a distinct supersession chain; resolving "
            "the collision means making all-but-one of these no longer carry "
            "the value."
        )
    )


class Tier3UniquenessActivation(BaseModel):
    """One (doc_type, field) pair whose partial UNIQUE index was created or
    confirmed by the migration."""

    doc_type: str = Field(description="Document type opted into uniqueness.")
    field: str = Field(description="The tier3_metadata field whose values are now unique.")


class MigrationReport(BaseModel):
    vault_id: VaultIdStr = Field(description="Identifier of the vault whose schema was inspected.")
    columns_added: list[MigrationReportEntry] = Field(
        description=(
            "Per-column entries for every ALTER TABLE that was applied. "
            "Empty when no schema work was pending."
        )
    )
    backfills_applied: list[str] = Field(
        description=(
            "Names of data backfills that were detected as pending and applied. "
            "Empty when no backfills were needed."
        )
    )
    tier3_uniqueness_activations: list[Tier3UniquenessActivation] = Field(
        default_factory=list,
        description=(
            "Per-(doc_type, field) entries for each `unique_keys` declaration "
            "whose underlying partial UNIQUE index was created or confirmed by "
            "this migration (T-0115). Empty when no `unique_keys` are declared "
            "or all were already active."
        ),
    )
    tier3_uniqueness_collisions: list[Tier3UniquenessCollision] = Field(
        default_factory=list,
        description=(
            "Per-(doc_type, field, value) entries for every collision that "
            "prevented activation of a `unique_keys` declaration (T-0115). "
            "Non-empty means at least one declaration is NOT live; the "
            "substrate refuses to activate the constraint while collisions "
            "remain (CAS-ADR-031 §5). The operator resolves via renumber, "
            "supersede, or archive-and-recreate, then re-runs the migration."
        ),
    )


class ReabstractRequest(BaseModel):
    include_pdf: bool = Field(
        default=False,
        description=(
            "When False (default), documents whose source_type is 'pdf' are "
            "skipped: scanned PDFs typically have no extractable text and "
            "reabstract returns a degenerate abstract. When True, PDFs are "
            "included in the worklist."
        ),
    )


class ReabstractReportEntry(BaseModel):
    document_id: DocumentIdStr = Field(
        description="Document id whose reabstract outcome this entry records."
    )
    outcome: ReabstractOutcome = Field(
        description="Per-document classification (success / skipped_pdf / llm_failure)."
    )
    error_message: str | None = Field(
        default=None,
        description="Failure description when outcome is 'llm_failure'; null otherwise.",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        description=(
            "Wall-clock seconds from dispatch to terminal status for this "
            "document; null for skipped_pdf entries (no work was done)."
        ),
    )


class ReabstractReport(BaseModel):
    vault_id: VaultIdStr = Field(
        description="Identifier of the vault whose deferred abstracts were processed."
    )
    reabstracted_count: int = Field(
        description=(
            "Number of documents whose pipeline_status transitioned to "
            "abstraction_complete by this operation."
        )
    )
    skipped_pdf_count: int = Field(
        description=(
            "Number of source_type=pdf documents excluded because include_pdf "
            "was False. Always 0 when include_pdf is True."
        )
    )
    failed_count: int = Field(
        description=(
            "Number of documents whose reabstract attempt did not reach abstraction_complete."
        )
    )
    entries: list[ReabstractReportEntry] = Field(
        description=(
            "Per-document outcome records. Length equals "
            "reabstracted_count + skipped_pdf_count + failed_count."
        )
    )


class DriftEntry(BaseModel):
    """Per-edge entry in a DriftReport.

    Surfaced when the recorded `synced_from_*` provenance on a
    `sync_target` / `derived_from` edge no longer matches the current
    head of the source chain (`content_drift`), the chain has advanced
    without content change (`chain_advanced_no_content_change`), the
    edge predates the columns (`recorded_null`), or the source
    chain is forked (`chain_nonlinear`). Hash is the authoritative
    comparator; version label is a display key.
    """

    edge_id: EdgeIdStr = Field(description="Identifier of the drifted edge.")
    edge_type: EdgeType = Field(
        description="`sync_target` or `derived_from` (the two provenance-bearing edge types)."
    )
    source_id: DocumentIdStr = Field(description="Edge source document id (the dependent).")
    target_id: DocumentIdStr = Field(
        description="Edge target document id (the canonical/source document)."
    )
    recorded_version_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "The `synced_from_version` recorded on the edge (a document "
            "id), or null when not recorded."
        ),
    )
    recorded_version_label: str | None = Field(
        default=None,
        description=(
            "The `version_label` of the recorded synced-from document at "
            "the time the drift report was generated, when resolvable."
        ),
    )
    recorded_content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "The `synced_from_content_hash` recorded on the edge, or null when not recorded."
        ),
    )
    current_head_id: DocumentIdStr | None = Field(
        default=None,
        description=(
            "Document id of the current head of the target's supersedes "
            "chain. Null when `staleness_basis = chain_nonlinear` "
            "(multiple heads — see `competing_head_count`)."
        ),
    )
    current_head_version_label: str | None = Field(
        default=None,
        description="Version label of the current chain head, when resolvable.",
    )
    current_head_content_hash: Sha256Str | None = Field(
        default=None,
        description=(
            "`source_content_hash` of the current chain head. Null on "
            "`chain_nonlinear` and on `recorded_null` rows where we did "
            "not need to compute it."
        ),
    )
    competing_head_count: int | None = Field(
        default=None,
        description=(
            "Number of heads observed when `staleness_basis = "
            "chain_nonlinear`; null otherwise. Operators follow up via "
            "`chain` for full forensics."
        ),
    )
    staleness_basis: StalenessBasis = Field(
        description="Why this edge appears in the drift report."
    )


class DriftReport(BaseModel):
    """Result of a `verify_vault_drift` call.

    Per-vault audit of `sync_target` / `derived_from` edges whose
    recorded provenance has diverged from the current source-chain head.
    `entries` includes one row per edge that is either drifted
    (`content_drift`), informationally noteworthy
    (`chain_advanced_no_content_change`, `recorded_null`), or carrying a
    data-quality issue (`chain_nonlinear`). Edges whose recorded
    provenance still matches the head are absent from the report.
    """

    vault_id: VaultIdStr = Field(description="Identifier of the vault whose edges were walked.")
    total_edges_walked: int = Field(
        description=(
            "Total count of active `sync_target` / `derived_from` edges "
            "the detector inspected. Equals the universe; `len(entries)` "
            "is the subset that triggered a report row."
        )
    )
    summary: dict[str, int] = Field(
        description=(
            "Per-basis counts. Keys are `StalenessBasis` enum values; "
            "each value is the count of `entries` carrying that basis."
        )
    )
    entries: list[DriftEntry] = Field(
        description=(
            "Per-edge report rows; one row per edge whose state warranted operator attention."
        )
    )


class ReabstractProgressEvent(BaseModel):
    """SSE `progress` event payload for reabstract-deferred.

    Emitted twice per non-PDF document (a `started` event before
    dispatch, then a `completed` or `failed` event after the polling
    loop reaches a terminal pipeline_status) and once per skipped PDF
    (a `skipped` event). Shape mirrors the ingest pipeline's
    `ProgressEvent` precedent (docs/fs/cas_app_api.openapi.yaml).
    """

    event_type: Literal["progress"] = Field(
        description="Discriminator for the SSE event payload variant; always 'progress'.",
    )
    processed: int = Field(
        description=(
            "Count of documents whose terminal event has already been "
            "emitted in this stream. Zero on the first `started` event; "
            "increments by one on each `completed`, `failed`, or `skipped` "
            "event. Equals `total` once the stream is exhausted."
        )
    )
    total: int = Field(
        description=(
            "Total document count in the reabstract-deferred worklist for "
            "this run, including PDFs that will be skipped when "
            "`include_pdf=False`. Constant across all events in the stream."
        )
    )
    current_document_id: DocumentIdStr = Field(description="Document id this event refers to.")
    current_title: str = Field(
        description=(
            "Human-readable title of the document this event refers to. "
            "Surfaced so the maintenance panel can show the user what is "
            "in flight without a separate get_document round-trip."
        )
    )
    status: Literal["started", "completed", "failed", "skipped"] = Field(
        description=(
            "Per-document status. `started`: dispatch begun, no outcome "
            "yet. `completed`: reabstract reached `abstraction_complete`. "
            "`failed`: dispatch raised or terminal pipeline_status was "
            "`failed` (outcome=`llm_failure`). `skipped`: PDF excluded "
            "from the worklist by `include_pdf=False` "
            "(outcome=`skipped_pdf`)."
        )
    )
    outcome: ReabstractOutcome | None = Field(
        default=None,
        description=(
            "Per-document terminal classification. Set on `completed`, "
            "`failed`, and `skipped` events; omitted on the leading "
            "`started` event."
        ),
    )
    error: str | None = Field(
        default=None,
        description="Failure description when `status=failed`; omitted otherwise.",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        description=(
            "Wall-clock seconds from dispatch to terminal status, set on "
            "`completed` and `failed` events. Omitted on `started` (no "
            "work yet) and `skipped` (no work was done)."
        ),
    )


class ReabstractSummaryEvent(BaseModel):
    """SSE `summary` event payload for reabstract-deferred.

    Emitted once at the end of the stream, after all per-document
    progress events. Payload fields (sans the `event_type`
    discriminator) are structurally identical to ReabstractReport so
    the MCP tool aggregator can derive its return dict directly from
    this event.
    """

    event_type: Literal["summary"] = Field(
        description="Discriminator for the SSE event payload variant; always 'summary'.",
    )
    vault_id: VaultIdStr = Field(
        description="Identifier of the vault whose deferred abstracts were processed."
    )
    reabstracted_count: int = Field(
        description=(
            "Number of documents whose pipeline_status transitioned to "
            "abstraction_complete by this run."
        )
    )
    skipped_pdf_count: int = Field(
        description=("Number of source_type=pdf documents excluded because include_pdf was False.")
    )
    failed_count: int = Field(
        description=(
            "Number of documents whose reabstract attempt did not reach abstraction_complete."
        )
    )
    entries: list[ReabstractReportEntry] = Field(
        description=(
            "Per-document outcome records. Length equals "
            "reabstracted_count + skipped_pdf_count + failed_count."
        )
    )


class EvalRetrievalResult(BaseModel):
    vault_id: VaultIdStr = Field(
        description="Identifier of the vault whose assertions were evaluated."
    )
    passed: bool = Field(description="True if all assertions passed.")
    assertion_count: int = Field(description="Total number of assertions evaluated.")
    failure_count: int = Field(description="Number of assertions that did not pass.")
    failures: list[AssertionFailure] = Field(description="Per-assertion failure details.")


# ---------------------------------------------------------------------------
# Vault listing and statistics (BE-001 through BE-006)
# ---------------------------------------------------------------------------


class VaultDocTypeEntry(BaseModel):
    value: str = Field(description='Doc type identifier (e.g. "design_spec").')
    label: str = Field(description="Human-readable label for UI display.")


class VaultLifecycleState(BaseModel):
    value: str = Field(description='Lifecycle state identifier (e.g. "active", "archived").')
    label: str = Field(description="Human-readable label for UI display.")
    is_terminal: bool = Field(
        default=False,
        description="Terminal states cannot be transitioned out of.",
    )


class VaultAdapterInfo(BaseModel):
    source_type: str = Field(description="Source artifact format the adapter handles.")
    enabled: bool = Field(
        description=(
            "Whether the adapter is enabled in the vault config. "
            "Disabled adapters surface in scan results as status "
            '"adapter_disabled".'
        )
    )
    extensions: list[str] = Field(
        description='File extensions handled by this adapter (e.g. [".md", ".markdown"]).'
    )


class VaultSummary(BaseModel):
    id: VaultIdStr = Field(description="Unique vault identifier.")
    name: str = Field(description="Human-readable vault name.")
    description: str | None = Field(
        default=None,
        description="Vault-config description text; null when no description was authored.",
    )
    storage_root: str = Field(description="Path to the vault's source-file storage root.")
    doc_types: list[VaultDocTypeEntry] = Field(
        default_factory=list,
        description="Doc-type vocabulary configured for this vault.",
    )
    lifecycle_states: list[VaultLifecycleState] = Field(
        default_factory=list,
        description="Lifecycle states configured for this vault, in display order.",
    )
    adapters: list[VaultAdapterInfo] = Field(
        default_factory=list,
        description=(
            "Source adapters configured for this vault, with per-adapter "
            "enablement and handled extensions."
        ),
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
    total_documents: int = Field(description="Total number of documents in the vault.")
    by_lifecycle_status: dict[str, int] = Field(description="Document count per lifecycle status.")
    by_doc_type: dict[str, int] = Field(description="Document count per doc_type.")
    by_source_type: dict[str, int] = Field(description="Document count per source_type.")
    total_edges: int = Field(description="Total number of production edges in the vault graph.")
    by_edge_type: dict[str, int] = Field(description="Edge count per edge_type.")
    staging_edge_count: int = Field(
        description="Number of edges in the Tier 2 staging table awaiting review."
    )
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
    exists: bool = Field(
        description="True when a document with this hash already exists in the vault."
    )
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

    vault: dict | None = Field(
        default=None,
        description=(
            "Replacement for the vault section of vault_config.yaml; "
            "structure per vault_config.schema.json. Omit to leave "
            "unchanged."
        ),
    )
    document_types: dict | None = Field(
        default=None,
        description=(
            "Replacement for the document_types section; structure per "
            "document_types.schema.json. Omit to leave unchanged."
        ),
    )
    lifecycle: dict | None = Field(
        default=None,
        description=(
            "Replacement for the lifecycle section; structure per "
            "lifecycle.schema.json. Omit to leave unchanged."
        ),
    )
    source_adapters: dict | None = Field(
        default=None,
        description=(
            "Replacement for the source_adapters section; structure per "
            "source_adapters.schema.json. Omit to leave unchanged."
        ),
    )
    metadata_extraction: dict | None = Field(
        default=None,
        description=(
            "Replacement for the metadata_extraction section; structure "
            "per metadata_extraction.schema.json. Omit to leave "
            "unchanged."
        ),
    )
    edge_inference: dict | None = Field(
        default=None,
        description=(
            "Replacement for the edge_inference section; structure per "
            "edge_inference.schema.json. Omit to leave unchanged."
        ),
    )
    abstraction: dict | None = Field(
        default=None,
        description=(
            "Replacement for the abstraction section of vault_config.yaml. Omit to leave unchanged."
        ),
    )
    access_control_defaults: dict | None = Field(
        default=None,
        description=(
            "Replacement for the access_control_defaults section of "
            "vault_config.yaml. Omit to leave unchanged."
        ),
    )
    retrieval_health: dict | None = Field(
        default=None,
        description=(
            "Replacement for the retrieval_health section of "
            "vault_config.yaml. Omit to leave unchanged."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152 / T-0163: When true, validate the merged config and "
            "compute the would-be projection of which sections would "
            "change plus destructive-change warnings, but do NOT write "
            "the yaml or reload the registry. The response carries "
            "`status='previewed'`, `dry_run=true`, `warnings` (always "
            "populated when present), and a `preview` listing which "
            "top-level sections would change. Note: vault-config updates "
            "are a config mutation, not a document field mutation, so "
            "the change surface is the existing `preview.changed_sections` "
            "field rather than a separate `changes` block (T-0163). "
            "**On dry-run, `force` is a no-op**: dry-run never raises "
            "`destructive_config_change`; warnings are always returned "
            "in the response body so the caller can plan a follow-up "
            "real-run with `force=true` if appropriate."
        ),
    )


class CreateVaultRequest(BaseModel):
    """Full config dict for new vault creation."""

    config: dict = Field(
        description=(
            "Full vault configuration object. Structure defined by "
            "``docs/fs/sage/vault_config.schema.json``; validation is "
            "performed against that schema before any filesystem writes."
        )
    )


class VaultConfigPreview(BaseModel):
    """Structural diff of a would-be vault config update.

    Populated only on dry-run responses; lists which top-level sections
    of the vault config would change if the request were committed.
    """

    changed_sections: list[str] = Field(
        description=(
            "Names of the top-level config sections (e.g., "
            "`document_types`, `lifecycle`, `source_adapters`) whose "
            "merged value differs from the currently-persisted value. "
            "Empty list when the request is a no-op (every supplied "
            "section is byte-identical to the current state)."
        )
    )


class UpdateVaultConfigResponse(BaseModel):
    """Returned after a vault config update (real-run or dry-run preview)."""

    status: Literal["updated", "previewed"] = Field(
        description=(
            "`updated` on a successful real-run write. `previewed` on a "
            "dry-run preview (T-0152); no yaml or registry state was "
            "modified."
        )
    )
    vault_id: VaultIdStr = Field(
        description="Id of the vault whose config was (or would be) updated."
    )
    warnings: list[str] = Field(
        description=(
            "Destructive-change warnings. Populated when `force=true` "
            "was used on a real-run to override the 409 rejection, OR "
            "when `dry_run=true` returned a preview that would trip "
            "destructive-change detection (T-0152: dry-run always "
            "surfaces warnings in the response body rather than raising)."
        )
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "T-0152: True when the request set `dry_run=true`; in that "
            "case no yaml or registry state was modified and `preview` "
            "carries the would-be projection of the config sections that "
            "would change. Note: vault-config updates are a config "
            "mutation, not a document field mutation, so the change "
            "surface is the existing `preview` field rather than a "
            "separate `changes` block (T-0163)."
        ),
    )
    preview: VaultConfigPreview | None = Field(
        default=None,
        description=(
            "T-0152: Structural diff populated on dry-run; null on a "
            "real-run. This is the would-be projection of the config "
            "sections that would change."
        ),
    )


# ---------------------------------------------------------------------------
# Staging edges (BE-010 through BE-013)
# ---------------------------------------------------------------------------


class StagingEdge(BaseModel):
    id: EdgeIdStr = Field(description="Unique identifier for this staging edge.")
    source_id: DocumentIdStr = Field(description="Origin document id for the proposed edge.")
    target_id: DocumentIdStr = Field(description="Target document id for the proposed edge.")
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
    created_at: datetime = Field(description="Timestamp when this staging edge was created.")


class StagingEdgeConfirmResponse(BaseModel):
    """Returned after promoting a staging edge to a production edge."""

    confirmed: Literal[True] = Field(
        description="Discriminator confirming the staging edge was promoted; always true."
    )
    staging_edge_id: EdgeIdStr = Field(
        description="Id of the staging edge that was promoted (now deleted)."
    )
    production_edge_id: EdgeIdStr = Field(description="Id of the production edge created.")


class StagingEdgeDismissResponse(BaseModel):
    """Returned after dismissing a staging edge."""

    dismissed: Literal[True] = Field(
        description="Discriminator confirming the staging edge was dismissed; always true."
    )
    staging_edge_id: EdgeIdStr = Field(
        description="Id of the staging edge that was dismissed (now deleted)."
    )


# ---------------------------------------------------------------------------
# Pending metadata (BE-014 through BE-015)
# ---------------------------------------------------------------------------


class ExtractedField(BaseModel):
    value: str | None = Field(
        default=None, description="The extracted value, or null if unavailable."
    )
    source: str = Field(description="How this field was derived.")
    alt_value: str | None = Field(default=None, description="Optional alternative candidate value.")
    alt_source: str | None = Field(default=None, description="Source label for alt_value.")


class PendingMetadataItem(BaseModel):
    document: Document = Field(description="The document awaiting metadata confirmation.")
    extracted_fields: dict[str, ExtractedField] = Field(
        description=(
            'Per-field annotation. Keys include "title", "doc_type", '
            '"project", "tags", "document_date" depending on which '
            "fields were extracted."
        )
    )


# ---------------------------------------------------------------------------
# Error response
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Uniform error envelope for all endpoints."""

    code: str = Field(
        description=(
            "Machine-readable error code (e.g., "
            '"invalid_lifecycle_transition", "document_not_found", '
            '"editor_permission_denied").'
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
