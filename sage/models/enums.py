"""Enumerations derived from the SAGE Core API OpenAPI specification.

`LifecycleStatus` and `LifecycleAction` are intentionally absent from this
module: vaults define domain-specific extensions to the base sets
(e.g., `filed` in Example Portfolio), so both surfaces are typed as `str` and
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
    PPTX = "pptx"


# Source types whose authoritative file is a binary container (a zipped OPC
# package, a PDF stream) rather than scannable text. A caller that receives
# the raw bytes of one of these and scans them for text tokens gets a
# confident false negative, because the readable content lives in the
# extracted-text projection, not the container bytes. The read path uses
# this partition to declare a body's form and to refuse handing back raw
# container bytes (CAS-ADR-039). Every ``SourceType`` not in this set is
# treated as text-form for body-form purposes.
BINARY_CONTAINER_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {
        SourceType.DOCX,
        SourceType.PDF,
        SourceType.XLSX,
        SourceType.PPTX,
    }
)


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
    ``search(target="edges", filters={"source_id":..., "edge_type":...})``
    . `merged_from` is a meta-edge recording that a successor chain
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
    typed, indexed column on the edges table so chain-repair and
    future inference rules filter via SQL rather than Python `startswith()`.

    `manual` is the default for edges with no recognized rationale prefix
    (hand-curated via create_edge, legacy edges without a prefix, edges
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

    `abstraction_interrupted` records abstraction work that was dropped
    rather than attempted: the queue draining it was stopped before the
    work ran or finished. It is terminal because nothing is left running
    to advance the document, and it is not a success -- `pipeline_error`
    carries the cause. Unlike the other non-success terminal status it
    says nothing about the document or the provider, so startup recovery
    re-enqueues it where it leaves `failed` alone.
    """

    PROJECTION_COMPLETE = "projection_complete"
    INDEXING_IN_PROGRESS = "indexing_in_progress"
    INDEXING_COMPLETE = "indexing_complete"
    ABSTRACTION_IN_PROGRESS = "abstraction_in_progress"
    ABSTRACTION_COMPLETE = "abstraction_complete"
    ABSTRACTION_SKIPPED = "abstraction_skipped"
    ABSTRACTION_INTERRUPTED = "abstraction_interrupted"
    FAILED = "failed"


class UserType(StrEnum):
    """Actor type for provenance and access control."""

    HUMAN = "human"
    AGENT = "agent"


class RetrievalMode(StrEnum):
    """Retrieval mode.

    Semantic returns ranked approximate results via vector + optional BM25
    fusion. Keyword returns BM25-only results, with terms conjunctive: a
    document matches only if it carries every term, though not necessarily
    together in one passage; a quoted phrase is the exception and must be
    satisfied within a single passage. Catalog returns all documents
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
    """Discriminates what ``search`` enumerates: documents, edges, or facets.

    `documents` (default) preserves the historical surface: results are
    ``DiscoverHit`` rows backed by ``DocumentSummary``. `edges` switches
    the dispatch to first-class edge enumeration: results are ``EdgeHit``
    rows carrying ``edge_id``, endpoints, edge_type, anchor versions,
    rationale, and retraction state. `facets` switches to vocabulary
    aggregation: results are ``FacetHit`` rows carrying the requested
    facet fields' top distinct values with counts (every facet field
    when none are selected), each row capped to a per-field value limit
    and carrying the field's true distinct-value total, so the response
    stays bounded at any corpus size and tagging density. `edges` and `facets` are only valid in
    combination with ``mode="catalog"``; other modes are rejected at
    request validation.
    """

    DOCUMENTS = "documents"
    EDGES = "edges"
    FACETS = "facets"


class FacetField(StrEnum):
    """The document metadata fields exposed for facet aggregation.

    The closed vocabulary of the ``facet_fields`` request parameter:
    ``search`` with ``target="facets"`` aggregates any subset of these,
    defaulting to all of them.
    """

    DOC_TYPE = "doc_type"
    LIFECYCLE_STATUS = "lifecycle_status"
    SOURCE_TYPE = "source_type"
    PIPELINE_STATUS = "pipeline_status"
    TAGS = "tags"


class ResponseMode(StrEnum):
    """Payload depth for ``search`` results (,).

    `light` returns identity columns only and omits rationale, retraction
    envelope, and other large fields. `full` returns the complete envelope.
    When unset, ``search`` applies a default-threshold rule
    (>5 results → light, otherwise full) so single-item calls keep their
    contextual richness while bulk enumerations stay inside the MCP inline
    budget. Canonical name across SAGE surfaces.
    """

    LIGHT = "light"
    FULL = "full"


# Shared threshold for the response_mode default-resolution rule. When a
# caller does not pass response_mode on a ``ResponseMode``-aware surface,
# result/batch size > this value defaults to LIGHT (so bulk enumerations
# stay inside the MCP inline-output budget); at or below it, default is
# FULL (so single-item-style calls keep their contextual richness).
#
# Originally tied to edge-enumeration default; extends
# the same rule to the bulk mutation tools (``bulk_update_metadata``
# and ``bulk_update_lifecycle``). The 5-item figure comes from
# field-use report (a 28-item bulk_update_metadata batch overflowed the
# MCP inline budget by returning a full ``semantic_abstract`` per item).
#
# The scope of this default is per-surface (see "Scope of the
# threshold-default stance" design note for why the rule is NOT applied
# to ``search`` document-target results).
LIGHT_DEFAULT_THRESHOLD = 5


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
    """Per-document outcome categories in a ReabstractReport.

    `still_skipped`, `timeout` and `interrupted` all count toward a
    report's `failed_count` -- the field counts documents that did not
    reach `abstraction_complete` -- but none of them is an
    `llm_failure`. A still-skipped document declined abstraction rather
    than attempting it; a timed-out one was abandoned by the waiter
    while the generation it was waiting on may still be running; an
    interrupted one had its work dropped by a stopped queue and never
    reached a provider at all. Folding any of them into `llm_failure`
    would send an operator looking for a provider error that never
    happened.
    """

    SUCCESS = "success"
    SKIPPED_PDF = "skipped_pdf"
    LLM_FAILURE = "llm_failure"
    STILL_SKIPPED = "still_skipped"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class StalenessBasis(StrEnum):
    """Per-edge drift classification in a DriftReport.

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
        PipelineStatus.ABSTRACTION_INTERRUPTED,
        PipelineStatus.FAILED,
    }
)

# The same set as raw ``pipeline_status`` strings. Every consumer that
# compares against a stored status wants these rather than the enum members,
# and each one deriving its own frozenset is how a restated subset drifts:
# a comparison that silently omits a terminal status leaves a poller waiting
# on a document that has already finished.
TERMINAL_PIPELINE_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in TERMINAL_PIPELINE_STATUSES
)

# Terminal pipeline statuses that represent success: the pipeline finished
# without an error. ``pipeline_error`` records the most recent failure, so a
# document that reaches one of these carries no failure and the field must be
# cleared as part of the transition.
SUCCESSFUL_TERMINAL_PIPELINE_STATUSES: frozenset[PipelineStatus] = frozenset(
    {
        PipelineStatus.ABSTRACTION_COMPLETE,
        PipelineStatus.ABSTRACTION_SKIPPED,
    }
)
