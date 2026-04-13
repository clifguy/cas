"""Enumerations derived from the SAGE Core API OpenAPI specification."""

from enum import StrEnum


class SourceType(StrEnum):
    MARKDOWN = "markdown"
    DOCX = "docx"
    PDF = "pdf"
    EMAIL = "email"
    ONENOTE = "onenote"
    TEAMS_CHAT = "teams_chat"
    XLSX = "xlsx"


class EdgeType(StrEnum):
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    COVERS = "covers"
    REFERENCES = "references"
    BUNDLES_WITH = "bundles_with"
    AUTHORITATIVE_FOR = "authoritative_for"
    DEPENDS_ON = "depends_on"
    SYNC_TARGET = "sync_target"


class PipelineStatus(StrEnum):
    PROJECTION_COMPLETE = "projection_complete"
    INDEXING_IN_PROGRESS = "indexing_in_progress"
    INDEXING_COMPLETE = "indexing_complete"
    ABSTRACTION_IN_PROGRESS = "abstraction_in_progress"
    ABSTRACTION_COMPLETE = "abstraction_complete"
    ABSTRACTION_SKIPPED = "abstraction_skipped"
    FAILED = "failed"


class UserType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class RetrievalMode(StrEnum):
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    DETERMINISTIC = "deterministic"
    VERIFICATION = "verification"
    CATALOG = "catalog"


class RetrievalScope(StrEnum):
    ALL = "all"
    AUTHORITATIVE = "authoritative"
    SPECIFIC = "specific"
    FILTERED = "filtered"


class CatalogSortBy(StrEnum):
    TITLE = "title"
    DOCUMENT_DATE = "document_date"
    LIFECYCLE_STATUS = "lifecycle_status"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class TraversalDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
    BOTH = "both"


# Terminal pipeline statuses: pipeline has finished processing.
TERMINAL_PIPELINE_STATUSES: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.ABSTRACTION_COMPLETE,
    PipelineStatus.ABSTRACTION_SKIPPED,
    PipelineStatus.FAILED,
})
