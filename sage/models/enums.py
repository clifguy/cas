"""Enumerations derived from the SAGE Core API OpenAPI specification.

`LifecycleStatus` and `LifecycleAction` are intentionally absent from this
module: vaults define domain-specific extensions to the base sets
(e.g., `filed` in PIM Health), so both surfaces are typed as `str` and
validated against vault config at the API boundary rather than at the
Python type system.
"""

from enum import StrEnum


class SourceType(StrEnum):
    """Source artifact format.

    Determines which source adapter processes the file during ingestion.
    """

    MARKDOWN = "markdown"
    DOCX = "docx"
    PDF = "pdf"
    EMAIL = "email"
    ONENOTE = "onenote"
    TEAMS_CHAT = "teams_chat"
    XLSX = "xlsx"


class EdgeType(StrEnum):
    """Typed, directed relationship between documents.

    See the SAGE Architecture Reference Section 4.6 for semantics and
    traversal use cases. Each edge type has a `resolution_policy` declared
    in the vault's `edge_type_registry` controlling how chain-scoped
    resolution treats it during traversal (CAS-ADR-017).

    `instantiated_from` models live-tracking derivation (e.g., a checklist
    instantiated from a template that should propagate template updates).
    `retracts` is a meta-edge pointing at an earlier edge instance that the
    retracting chain now disclaims. The retracting edge requires the
    `edge_id` of the edge it disclaims; that id is discoverable via
    ``sage_discover(target="edges", filters={"source_id": ..., "edge_type": ...})``
    (T-0157). `merged_from` is a meta-edge recording that a successor chain
    absorbs predecessor chains, tombstoning their downstream edges.
    """

    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    INSTANTIATED_FROM = "instantiated_from"
    COVERS = "covers"
    REFERENCES = "references"
    BUNDLES_WITH = "bundles_with"
    AUTHORITATIVE_FOR = "authoritative_for"
    DEPENDS_ON = "depends_on"
    SYNC_TARGET = "sync_target"
    RETRACTS = "retracts"
    MERGED_FROM = "merged_from"


class RationaleKind(StrEnum):
    """Typed discriminator for the provenance of an edge's `rationale` text.

    Promoted from the rationale-text prefix convention introduced in
    CAS-ADR-019 (auto edge inference may delete only its own edges) to a
    typed, indexed column on the edges table (T-0080) so chain-repair and
    future inference rules filter via SQL rather than Python `startswith()`.

    `manual` is the default for edges with no recognized rationale prefix
    (hand-curated via sage_link, legacy edges without a prefix, edges
    written before this column was added). The column is NOT NULL with
    DEFAULT 'manual' in storage.
    """

    VERSION_CHAIN = "version_chain"
    REFERENCES_MENTION = "references_mention"
    FILENAME_CODE_MATCH = "filename_code_match"
    MANUAL = "manual"


class ResolutionPolicy(StrEnum):
    """How the chain-scoped edge resolver treats an edge type during traversal (CAS-ADR-017).

    `none`: the edge is a lineage fact, not a propagating relationship
    (meta-edges: supersedes, retracts, merged_from).

    `transitive_source`: the edge's source anchor must lie in the queried
    document's supersedes lineage; target endpoint is frozen at derivation
    (no target anchor; `target_valid_from_version` is null) (e.g.,
    `derived_from`).

    `transitive_target`: mirror of `transitive_source`. The edge's target
    anchor must lie in the queried document's supersedes lineage; source
    endpoint is frozen at derivation (no source anchor;
    `source_valid_from_version` is null).

    `transitive_both`: both source and target anchors must lie in their
    respective chains' lineages relative to the query document (e.g.,
    `covers`, `references`, `bundles_with`, `depends_on`,
    `instantiated_from`).

    `TBD`: policy not yet frozen. Any attempt to create or migrate an edge
    with a TBD policy raises a write-time error.
    """

    NONE = "none"
    TRANSITIVE_SOURCE = "transitive_source"
    TRANSITIVE_TARGET = "transitive_target"
    TRANSITIVE_BOTH = "transitive_both"
    TBD = "TBD"


class PipelineStatus(StrEnum):
    """Tracks progress of the three-stage ingestion pipeline.

    `abstraction_skipped` indicates the vault has abstraction disabled or
    the LLM was unavailable (graceful degradation per CAS-ADR-011).
    """

    PROJECTION_COMPLETE = "projection_complete"
    INDEXING_IN_PROGRESS = "indexing_in_progress"
    INDEXING_COMPLETE = "indexing_complete"
    ABSTRACTION_IN_PROGRESS = "abstraction_in_progress"
    ABSTRACTION_COMPLETE = "abstraction_complete"
    ABSTRACTION_SKIPPED = "abstraction_skipped"
    FAILED = "failed"


class UserType(StrEnum):
    """Actor type for provenance and access control."""

    HUMAN = "human"
    AGENT = "agent"


class RetrievalMode(StrEnum):
    """Retrieval mode.

    Semantic returns ranked approximate results via vector + optional BM25
    fusion. Keyword returns BM25-only results. Catalog returns all documents
    matching filter predicates via direct SQL query (no vector search, no
    chunk content, supports pagination via offset). Deterministic returns
    exact content by address.
    """

    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    DETERMINISTIC = "deterministic"
    CATALOG = "catalog"


class RetrievalScope(StrEnum):
    """Controls which documents are eligible for retrieval, independent of mode."""

    ALL = "all"
    AUTHORITATIVE = "authoritative"
    SPECIFIC = "specific"
    FILTERED = "filtered"


class RetrievalTarget(StrEnum):
    """Discriminates whether ``sage_discover`` enumerates documents or edges (T-0157).

    `documents` (default) preserves the historical surface: results are
    ``DiscoverHit`` rows backed by ``DocumentSummary``. `edges` switches
    the dispatch to first-class edge enumeration: results are ``EdgeHit``
    rows carrying ``edge_id``, endpoints, edge_type, anchor versions,
    rationale, and retraction state. Only valid in combination with
    ``mode="catalog"``; other modes are rejected at request validation.
    """

    DOCUMENTS = "documents"
    EDGES = "edges"


class ResponseMode(StrEnum):
    """Payload depth for ``sage_discover`` results (T-0157, T-0153).

    `light` returns identity columns only and omits rationale, retraction
    envelope, and other large fields. `full` returns the complete envelope.
    When unset, ``sage_discover`` applies a default-threshold rule
    (>5 results → light, otherwise full) so single-item calls keep their
    contextual richness while bulk enumerations stay inside the MCP inline
    budget. Canonical name across SAGE surfaces; supersedes the
    document-target-only ``response_level`` parameter for new callers.
    """

    LIGHT = "light"
    FULL = "full"


class ResponseLevel(StrEnum):
    """Controls the detail level of discover results.

    `chunks` (default) includes `chunk_content`, `heading_path`, and
    `matched_chunk_count` on each hit. `documents` suppresses
    `chunk_content` but preserves `heading_path` (best-scoring chunk's
    location, cheap "why this matched" context), `relevance_score`, and
    `matched_chunk_count`. Applicable to semantic and keyword modes.
    Catalog mode always returns document-level results regardless of this
    value. Deterministic mode always returns chunk content regardless of
    this value.
    """

    CHUNKS = "chunks"
    DOCUMENTS = "documents"


class CatalogSortBy(StrEnum):
    """Sort key for catalog mode results. Ignored by other retrieval modes."""

    TITLE = "title"
    DOC_TYPE = "doc_type"
    DOCUMENT_DATE = "document_date"
    LIFECYCLE_STATUS = "lifecycle_status"


class SortOrder(StrEnum):
    """Sort direction for catalog mode results. Ignored by other modes."""

    ASC = "asc"
    DESC = "desc"


class TraversalDirection(StrEnum):
    """Edge traversal direction relative to the starting document."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"
    BOTH = "both"


class ReabstractOutcome(StrEnum):
    """Per-document outcome categories in a ReabstractReport (T-0089)."""

    SUCCESS = "success"
    SKIPPED_PDF = "skipped_pdf"
    LLM_FAILURE = "llm_failure"


class StalenessBasis(StrEnum):
    """Per-edge drift classification in a DriftReport (T-0111).

    Used to discriminate why a `sync_target` / `derived_from` edge appears
    in the drift report. `content_drift` is the load-bearing "stale, act
    now" signal. `chain_advanced_no_content_change` and `recorded_null`
    are informational. `chain_nonlinear` is a data-quality flag, not a
    drift signal.
    """

    CONTENT_DRIFT = "content_drift"
    CHAIN_ADVANCED_NO_CONTENT_CHANGE = "chain_advanced_no_content_change"
    RECORDED_NULL = "recorded_null"
    CHAIN_NONLINEAR = "chain_nonlinear"


# Terminal pipeline statuses: pipeline has finished processing.
TERMINAL_PIPELINE_STATUSES: frozenset[PipelineStatus] = frozenset(
    {
        PipelineStatus.ABSTRACTION_COMPLETE,
        PipelineStatus.ABSTRACTION_SKIPPED,
        PipelineStatus.FAILED,
    }
)
