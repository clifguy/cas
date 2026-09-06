"""Retrieval service: semantic, keyword, and deterministic discover modes.

Covers behavioral tests BH-020, BH-027 through BH-030, BH-059 through BH-061,
BH-069, BH-070, BH-084 through BH-088.

Semantic mode:
  - Pure vector search (default) or hybrid vector+BM25 via RRF (BH-027, BH-028).
  - Filters by lifecycle_status != failed pipeline (BH-020).
  - Scope gating: all, authoritative, specific, filtered.

Keyword mode (BH-059, BH-060, BH-061):
  - BM25-only search. No query embedding required.
  - Same pipeline and scope gating as semantic mode.

Deterministic mode:
  - Exact heading path prefix match within a single document (BH-029).
  - Rejects documents with failed pipeline (BH-021, via pipeline gate).
  - Returns 404 for non-existent heading paths (BH-030).
"""

import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone

from sage.adapters.interfaces import (
    DOCUMENT_FACET_FIELDS,
    ContentStore,
    EmbeddingProvider,
    FacetFieldCounts,
    GraphStore,
    SearchResult,
    StorageQueryError,
)
from sage.api.errors import (
    DocumentNotFoundError,
    HeadingNotFoundError,
    MissingFieldError,
    PipelineIncompleteError,
    StorageQueryFailedError,
    Tier3SchemaViolationError,
)
from sage.config import VaultConfig
from sage.instrumentation.timing import (
    NULL_QUERY_TIMER,
    NullQueryTimer,
    PhaseCollector,
    QueryTimer,
    _NullPhaseCollector,
)
from sage.models.enums import (
    LIGHT_DEFAULT_THRESHOLD,
    PipelineStatus,
    ResponseMode,
    RetrievalMode,
    RetrievalScope,
    RetrievalTarget,
)
from sage.models.graph_rows import EdgeQueryRow
from sage.models.schemas import (
    DiscoverHit,
    DiscoverRequest,
    DiscoverResponse,
    Document,
    DocumentSummary,
    DocumentSummaryLight,
    EdgeHit,
    FacetHit,
)
from sage.services.read_diagnostics import build_not_found_detail
from sage.utils.date_parsing import parse_document_date
from sage.utils.rrf import rrf_fuse

logger = logging.getLogger(__name__)

# Filter keys the content store can pre-filter on as chunk-row columns. Pure
# pushdown sets (every active key in this set) bypass the graph-store
# document_id IN-clause resolution entirely. Keep in sync with
# ``_FILTERABLE_COLUMNS`` in sage/adapters/content_store_postgres.py.
_CHUNK_PUSHDOWN_KEYS = frozenset({"doc_type", "lifecycle_status", "project"})

# Over-fetch multipliers applied to ``DiscoverRequest.limit`` when
# computing how many candidate chunks the content store returns before
# post-search dedup, RRF re-rank, and min_relevance culls. See
# ``RetrievalService._fetch_limit`` for the tier semantics.
_FETCH_MULTIPLIER_NONE = 5
_FETCH_MULTIPLIER_PUSHDOWN = 3
_FETCH_MULTIPLIER_MIXED = 10

# Default budget (bytes) below which the Claude Code MCP runtime will
# deliver a tool result inline; above it the runtime falls back to the
# disk/jq round-trip. The default is empirical; override per-process via
# the ``SAGE_MCP_INLINE_BUDGET_BYTES`` environment variable. Catalog mode
# attaches a ``recommended_limit`` hint when the serialized response
# exceeds this budget.
DEFAULT_MCP_INLINE_BUDGET_BYTES = 24576

# Safety factor applied when computing ``recommended_limit`` so the
# re-paged response stays comfortably under budget. The hint itself adds
# bytes the first measurement does not see; a 5% margin absorbs that.
_BUDGET_RECOMMEND_SAFETY_FACTOR = 0.95

# Per-field value cap applied to facet aggregation when the request
# carries no explicit ``facet_value_limit``. Declared vocabularies sit
# far below this, so the cap only ever bites free-text tags -- the one
# facet whose vocabulary grows with the corpus. The default keeps the
# no-options orientation call bounded at any corpus size; each row's
# ``total_distinct`` makes the truncation visible and names the limit
# that reaches the full vocabulary.
DEFAULT_FACET_VALUE_LIMIT = 50


def _resolve_mcp_inline_budget_bytes() -> int:
    """Resolve the MCP inline budget per call (not at import).

    Mirrors the pattern from ``sage/services/documents.py``'s
    ``SAGE_MAX_INLINE_CONTENT_BYTES`` resolver: read the env var each
    call so tests (and operators) can override without restarting the
    process. Invalid or non-positive values fall back to the default.
    """
    raw = os.environ.get("SAGE_MCP_INLINE_BUDGET_BYTES")
    if not raw:
        return DEFAULT_MCP_INLINE_BUDGET_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MCP_INLINE_BUDGET_BYTES
    return value if value > 0 else DEFAULT_MCP_INLINE_BUDGET_BYTES


def _apply_catalog_budget_hint(response: DiscoverResponse) -> None:
    """Annotate the response with a budget hint when it exceeds the inline ceiling.

    Measures the serialized response in bytes via the same encoding the
    MCP runtime uses (Pydantic ``model_dump(mode="json", exclude_none=True)``
    then UTF-8 JSON). When the size is over budget, computes a
    ``recommended_limit`` proportional to the number of records that
    would fit, and merges the hint into ``response.hints``. No-op when
    there are no results — empty responses are trivially inline.

    Advisory only — the response is not truncated.
    """
    if not response.results:
        return
    size = len(json.dumps(response.model_dump(mode="json", exclude_none=True)).encode("utf-8"))
    budget = _resolve_mcp_inline_budget_bytes()
    if size <= budget:
        return
    recommended = max(
        1,
        int(len(response.results) * budget / size * _BUDGET_RECOMMEND_SAFETY_FACTOR),
    )
    budget_hint: dict[str, object] = {
        "reason": "response_exceeds_inline_budget",
        "response_size_bytes": size,
        "budget_bytes": budget,
        "recommended_limit": recommended,
    }
    if response.hints is None:
        response.hints = budget_hint
    else:
        response.hints = {**response.hints, **budget_hint}


class RetrievalService:
    def __init__(
        self,
        graph_store: GraphStore,
        content_store: ContentStore,
        embedding_provider: EmbeddingProvider,
        config: VaultConfig,
        *,
        query_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
    ) -> None:
        self._graph = graph_store
        self._content = content_store
        self._embedding = embedding_provider
        self._config = config
        self._query_timer = query_timer

    @staticmethod
    def _fetch_limit(request: DiscoverRequest) -> int:
        """Compute content store fetch limit.

        Over-fetch headroom compensates for post-search culls:
        document-level dedup (one hit per doc), the inner RRF re-rank
        fetch multiplier, and the optional ``min_relevance`` threshold.
        The size of the headroom depends on what kind of filtering the
        candidate set has already been through:

        * No filters → 5x. Dedup-only headroom.
        * All pushdownable (``doc_type``, ``lifecycle_status``,
          ``project``) → 3x. The content store has already filtered at the
          column level, so the only remaining cull is dedup.
        * Mixed (any of ``tags``, ``pipeline_status``, ``document_ids``,
          ``tier3_metadata``) → 10x. Graph-resolved ``document_id`` IN
          clause may
          bloat the candidate set when the resolved doc set is large;
          conservative backstop keeps recall high.
        """
        if not request.filters:
            return request.limit * _FETCH_MULTIPLIER_NONE
        f = request.filters
        has_any_filter = bool(
            f.doc_type
            or f.lifecycle_status
            or f.project
            or f.tags
            or f.pipeline_status
            or f.document_ids
            or f.source_type
            or f.tier3_metadata
        )
        if not has_any_filter:
            return request.limit * _FETCH_MULTIPLIER_NONE
        is_mixed = bool(
            f.tags or f.pipeline_status or f.document_ids or f.source_type or f.tier3_metadata
        )
        return request.limit * (_FETCH_MULTIPLIER_MIXED if is_mixed else _FETCH_MULTIPLIER_PUSHDOWN)

    async def _content_filters(
        self,
        request: DiscoverRequest,
    ) -> tuple[dict[str, str | list[str]] | None, bool]:
        """Build the pre-search filter dict for the content store.

        Resolves all metadata filters into a content-store pre-filter so
        the content-store top-K cutoff operates on the correct corpus. Two
        kinds of filters merge here:

        * Chunk-level (``_CHUNK_PUSHDOWN_KEYS``): ``doc_type``,
          ``lifecycle_status``, and ``project`` are stored on each chunk
          row. Passed through directly to the content store as column
          predicates (for ``doc_type``, for the other two).
        * Document-level: ``tags``, ``pipeline_status``,
          ``document_ids``, ``tier3_metadata``. Resolved against the
          graph store into a list of matching ``document_id`` values
          which is then attached to the chunk filter as an IN clause.

        Returns ``(filters, has_doc_constraints)``. ``has_doc_constraints``
        is True when a graph-store resolution ran and might have produced
        an empty ``document_id`` list. When True and the resolved
        ``document_id`` list is empty, callers short-circuit to an empty
        result rather than running an unfiltered chunk search. Pure
        pushdown filter sets do not need the short-circuit because
        the content store returns zero rows naturally when no chunk matches.
        """
        if not request.filters:
            return None, False

        f = request.filters
        result: dict[str, str | list[str]] = {}
        if f.doc_type:
            result["doc_type"] = f.doc_type
        if f.lifecycle_status:
            result["lifecycle_status"] = f.lifecycle_status
        if f.project:
            result["project"] = f.project

        # Validate tier3 filter keys against the resolved doc_type's
        # metadata_schema even on the pure-pushdown path (defense in
        # depth — a tier3 filter is non-pushdownable today, so this
        # branch is unreachable, but the validation belongs with the
        # tier3 presence check rather than with the SQL path).
        if f.tier3_metadata:
            self._validate_tier3_filter_keys(f.tier3_metadata, f.doc_type)

        # Non-pushdownable filters still need a graph-store SQL
        # resolution into a document_id IN clause.
        has_non_pushdown = bool(
            f.tags or f.pipeline_status or f.document_ids or f.source_type or f.tier3_metadata
        )
        if has_non_pushdown:
            sql_filters: dict[str, object] = {}
            if f.doc_type:
                sql_filters["doc_type"] = f.doc_type
            if f.lifecycle_status:
                sql_filters["lifecycle_status"] = f.lifecycle_status
            if f.project:
                sql_filters["project"] = f.project
            if f.pipeline_status:
                sql_filters["pipeline_status"] = f.pipeline_status
            if f.tags:
                sql_filters["tags"] = f.tags
            if f.document_ids:
                sql_filters["document_ids"] = f.document_ids
            if f.source_type:
                sql_filters["source_type"] = f.source_type.value
            if f.tier3_metadata:
                sql_filters["tier3_metadata"] = f.tier3_metadata
            # Filter resolution wants the full match set, not a page;
            # use an unbounded limit. The SQL ceiling is the documents
            # table size pre-filter, which is bounded at vault scale.
            matching_docs, _ = await self._graph.query_documents(
                filters=sql_filters or None,
                limit=10_000_000,
                offset=0,
            )
            result["document_id"] = [doc.id for doc in matching_docs]

        return (result or None, has_non_pushdown)

    @staticmethod
    def _active_filters(request: DiscoverRequest) -> dict[str, object]:
        """The filter fields actually constraining this request.

        A ``RetrievalFilters`` instance is truthy whatever its fields hold, so
        presence of the object says nothing about whether anything was
        constrained. Everything reporting on active filters reads this, rather
        than the object, so a response cannot both name a filter scope and list
        no filters.
        """
        if not request.filters:
            return {}
        f = request.filters
        active: dict[str, object] = {}
        if f.doc_type:
            active["doc_type"] = f.doc_type
        if f.project:
            active["project"] = f.project
        if f.lifecycle_status:
            active["lifecycle_status"] = f.lifecycle_status
        if f.tags:
            active["tags"] = f.tags
        if f.document_ids:
            active["document_ids"] = f.document_ids
        if f.pipeline_status:
            active["pipeline_status"] = f.pipeline_status
        if f.source_type:
            active["source_type"] = f.source_type.value
        if f.tier3_metadata:
            active["tier3_metadata"] = f.tier3_metadata
        return active

    @staticmethod
    def _build_hints(
        raw_count: int,
        request: DiscoverRequest,
    ) -> dict[str, object] | None:
        """Build hints dict when results are empty.

        Two scenarios:
        * ``raw_count > 0``: chunks were fetched but post-filtering culled
          them. Hints surface ``total_before_filtering`` so callers see
          the gap.
        * ``raw_count == 0`` AND filters were active: pre-resolution
          found zero matching documents, so no chunks were even searched.
          Hints surface the active filters with ``total_before_filtering=0``
          so callers see what filtered out.
        * ``raw_count == 0`` and no filters: no useful hints — return None.
        """
        if raw_count == 0 and not request.filters:
            return None
        hints: dict[str, object] = {"total_before_filtering": raw_count}
        active = RetrievalService._active_filters(request)
        if active:
            hints["active_filters"] = active
        if request.scope and request.scope != RetrievalScope.ALL:
            hints["scope"] = request.scope.value
        return hints

    def _vocabulary_warnings(self, request: DiscoverRequest) -> list[str]:
        """Advisories for filter values outside this vault's vocabularies.

        doc_type and lifecycle_status draw their accepted values from
        vault config, not from a Python enum, so an unrecognized value
        cannot be refused at validation time the way an enum-typed
        filter is. Left unremarked it returns a successful empty
        response that a caller cannot tell apart from a defined value
        with no matches -- so the negative is only trustworthy if they
        first build their own positive control.

        Keyed on vocabulary membership, never on the result count: a
        recognized value that simply matches nothing is a true zero and
        must stay silent.
        """
        if not request.filters:
            return []
        warnings: list[str] = []
        checks = (
            ("doc_type", request.filters.doc_type, self._config.valid_doc_type_values()),
            (
                "lifecycle_status",
                request.filters.lifecycle_status,
                self._config.valid_lifecycle_status_values(),
            ),
        )
        for field, value, vocabulary in checks:
            if value and value not in vocabulary:
                warnings.append(
                    f"Filter {field}={value!r} is not among this vault's "
                    f"configured values: {sorted(vocabulary)!r}. No document "
                    f"can carry it, so the empty result reflects the filter "
                    f"rather than the vault's contents."
                )
        return warnings

    @staticmethod
    def _merge_hints(response: DiscoverResponse, addition: dict[str, object]) -> None:
        """Fold extra keys into a response's hints without discarding it."""
        if response.hints is None:
            response.hints = dict(addition)
            return
        merged = {**response.hints, **addition}
        # Advisories accumulate. Several can apply to one response and they are
        # attached from different points -- the mode handler knows what its own
        # search did, the dispatcher knows the request's filter vocabulary --
        # so a plain key overwrite would keep only whichever ran last.
        existing_warnings = response.hints.get("warnings")
        added_warnings = addition.get("warnings")
        if existing_warnings and added_warnings:
            merged["warnings"] = [*existing_warnings, *added_warnings]
        response.hints = merged

    # A single term carries no conjunction to explain; below this the empty
    # result is a plain miss and an advisory would misdirect.
    _MIN_CONJUNCTION_TERMS = 2

    async def _keyword_search_warnings(self, request: DiscoverRequest, raw_count: int) -> list[str]:
        """Advisory for a keyword term search that ran and matched nothing.

        Keyword mode is conjunctive: a document matches only if its text
        carries every term. An empty result therefore reads two ways -- the
        vault holds nothing on the subject, or the query asked for too many
        terms at once -- and nothing in the response distinguishes them, so a
        caller who did not build a positive control reads it as the former.

        Called only from the term-search path, with the row count that search
        returned, and only for a response that came back empty. All three
        conditions are load-bearing. A caller-supplied filter that resolves to
        no documents short-circuits before any search runs, and post-filtering
        (``min_relevance``) can empty a result the search did fill; in either
        case the response is empty while documents carrying the terms may well
        exist, so an advisory keyed on the response alone would assert a fact
        about the corpus that nothing established. The count alone is no more
        sufficient in the other direction: a response can carry documents the
        search never counted, and a sentence explaining an empty result has
        nothing to explain beside them. The third is applied by the caller
        rather than here, because the response is the one thing this helper
        cannot see.

        The terms come from the backend's own parse, because the words typed
        are not the terms required -- stopwords are dropped and the rest
        stemmed. Two shapes get their own treatment: a query the backend read
        as admitting alternatives is not conjunctive and gets no conjunction
        advisory, and a query whose every word was discarded gets an advisory
        of its own, since nothing was searched for at all.
        """
        if raw_count:
            return []
        query = (request.query or "").strip()
        # "*" is the filter-only listing form; it never reaches the term search.
        if not query or query == "*":
            return []
        parse = await self._content.parse_keyword_query(query)

        # Under an active constraint the miss is established only within the
        # slice it selected, so the sentence must not claim more than that.
        scope = " within the active filters" if self._active_filters(request) else ""

        if not parse.terms:
            # A query that rendered only exclusions did search -- for chunks
            # lacking those terms -- so it is not the discarded-input case.
            if parse.excluded:
                rendered = ", ".join(repr(t) for t in parse.excluded)
                return [
                    f"This query asked only for absences ({rendered}) and matched "
                    f"nothing{scope}, which means the terms it asked to avoid are "
                    "everywhere. Add a term to search for, rather than only terms "
                    "to avoid."
                ]
            return [
                "This query carried no searchable terms: every word in it was discarded "
                "by the vault's text-search configuration as a stopword or punctuation, "
                'so nothing was searched for. Use a distinctive term, or mode="semantic", '
                "which does not drop words."
            ]

        if not parse.all_required or len(parse.terms) < self._MIN_CONJUNCTION_TERMS:
            return []

        rendered = ", ".join(repr(t) for t in parse.terms)
        if parse.adjacent:
            # The chunk is named here and nowhere else: a phrase is the one
            # predicate still scoped to a single passage, because adjacency
            # across a passage boundary is not meaningful. It is also stricter
            # than a conjunction -- a passage can carry every term and still
            # miss -- so telling such a caller to quote a phrase would name
            # the thing they already did.
            return [
                f"This query parsed to {len(parse.terms)} terms ({rendered}) that a single "
                f"passage must carry adjacently, in that order, because the query "
                f"contains a phrase. No passage does{scope} -- terms held apart, or "
                "split across two passages of one document, do not match. Try "
                'unquoting the phrase, using fewer terms, or mode="semantic".'
            ]
        # No remedy here may make the query stricter. The match is already
        # over the whole document, so quoting a phrase -- which adds an
        # adjacency requirement inside a single chunk -- can only narrow a
        # result that is already empty.
        return [
            f"Keyword mode is conjunctive: this query parsed to {len(parse.terms)} terms "
            f"({rendered}) and matches only a document carrying all of them, though "
            f"not necessarily together in one passage. No document{scope} does, so "
            "the empty result may reflect the query rather than the vault's "
            'contents. Try fewer or more distinctive terms, or use mode="semantic".'
        ]

    def _apply_warnings(self, response: DiscoverResponse, request: DiscoverRequest) -> None:
        """Attach the request-scoped advisories, accumulating onto any already present.

        Search-scoped advisories are attached by the mode handler that ran the
        search, which is the only place that knows what it did; ``_merge_hints``
        accumulates the ``warnings`` key so the two do not displace each other.
        """
        warnings = self._vocabulary_warnings(request)
        if warnings:
            self._merge_hints(response, {"warnings": warnings})

    async def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        """Dispatch to the appropriate retrieval mode handler.

        Every graph-store query this service issues is reachable only from
        here, so this is where a backend query refusal is translated into
        the public envelope. The driver's message names the failing
        statement and is logged rather than returned; see
        ``StorageQueryFailedError``.
        """
        try:
            return await self._dispatch(request)
        except StorageQueryError as exc:
            logger.error(
                "storage query %s failed during %s retrieval: %s",
                exc.operation,
                request.mode.value,
                exc.driver_message,
            )
            raise StorageQueryFailedError(exc.operation) from exc

    async def _dispatch(self, request: DiscoverRequest) -> DiscoverResponse:
        """Route a request to its mode handler and apply shared post-processing."""
        request_id = uuid.uuid4().hex[:12]
        with self._query_timer.request(request.mode.value, request_id) as phases:
            # Edge enumeration bypasses the document-target post-
            # processing pipeline. The DiscoverRequest validator already
            # enforces target=edges <=> mode=catalog and rejects all
            # document-only knobs, so we can route directly.
            if request.target == RetrievalTarget.EDGES:
                response = await self._catalog_edges(request, phases)
                self._apply_warnings(response, request)
                _apply_catalog_budget_hint(response)
                return response

            # Facet aggregation likewise bypasses the document-target
            # post-processing pipeline. No budget hint: the response is
            # bounded by construction -- vocabulary fields by vocabulary
            # size, tags by the per-field value cap -- and the hint's
            # recommended_limit would name a parameter the facets
            # validator rejects.
            if request.target == RetrievalTarget.FACETS:
                response = await self._catalog_facets(request, phases)
                self._apply_warnings(response, request)
                return response

            if request.mode == RetrievalMode.SEMANTIC:
                response = await self._semantic(request, phases)
            elif request.mode == RetrievalMode.KEYWORD:
                response = await self._keyword(request, phases)
            elif request.mode == RetrievalMode.DETERMINISTIC:
                response = await self._deterministic(request, phases)
            elif request.mode == RetrievalMode.CATALOG:
                response = await self._catalog(request, phases)

            # Relevance threshold: drop scored results below min_relevance.
            # Unscored results (catalog, deterministic) are always kept.
            with phases.phase("post_filter_min_relevance"):
                if request.min_relevance is not None:
                    response.results = [
                        h
                        for h in response.results
                        if h.relevance_score is None or h.relevance_score >= request.min_relevance
                    ]
                    response.total_available = len(response.results)

                if not request.include_abstracts:
                    for hit in response.results:
                        # DocumentSummaryLight has no
                        # semantic_abstract field by design; skip the
                        # null-out so the assignment doesn't raise.
                        if isinstance(hit.document, DocumentSummary):
                            hit.document.semantic_abstract = None

            # Applied here rather than in _build_hints: that helper runs
            # only in semantic and keyword mode and only when the result
            # set is empty, whereas a filter value the vault does not
            # recognize is worth reporting in every mode.
            self._apply_warnings(response, request)

            # Surface a recommended_limit hint when a catalog
            # response would bust the Claude Code MCP inline ceiling.
            # Applied here (post-projection) so the byte measurement
            # reflects what the wire actually carries -- which is why it
            # runs last, after every other hint is attached.
            if request.mode == RetrievalMode.CATALOG:
                _apply_catalog_budget_hint(response)

            return response

    def _validate_tier3_filter_keys(
        self,
        tier3_metadata: dict,
        doc_type: str | None,
    ) -> None:
        """Reject tier3 filter keys that are not declared by the resolved
        doc_type's metadata_schema.

        Runs only when ``doc_type`` is supplied AND that doc_type has a
        metadata_schema declared. When the caller filters by tier3
        without a doc_type, this check is skipped -- the storage-layer
        format-regex check at ``_TIER3_KEY_FORMAT`` is the only fence.
        """
        if not doc_type:
            return
        validator = self._config.tier3_validator(doc_type)
        if validator is None:
            return
        declared = set(validator.schema.get("properties", {}).keys())
        for key in tier3_metadata:
            if key not in declared:
                raise Tier3SchemaViolationError(
                    doc_type=doc_type,
                    path=f"$.{key}",
                    message=(
                        f"tier3 filter key '{key}' is not declared in the "
                        f"metadata_schema for doc_type '{doc_type}'. "
                        f"Declared keys: {sorted(declared)!r}"
                    ),
                )

    # ------------------------------------------------------------------
    # Catalog mode (BH-072 through BH-079)
    # ------------------------------------------------------------------

    async def _catalog(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        """Filter-only document enumeration via SQL. No vector search.

        Queries the graph store directly with filter predicates. Returns
        document-level metadata only (no chunk content or relevance scores).
        Supports pagination via limit + offset.

        ``RetrievalFilters.tier3_metadata`` is pushed into SQL, with the
        predicate chosen per value so that every JSON type a
        ``metadata_schema`` can declare is filterable. A string compares
        through the ``tier3_metadata->>'<key>' = %s`` text accessor, which
        is what the expression indexes on the high-frequency canonical
        keys (``ticket_id``, ``failure_id``, ``tool_name``) are built on.
        Any other value compares as jsonb through the ``->`` accessor,
        since the text accessor cannot be compared against a natively
        typed parameter and rendering the value to text would not survive
        numeric formatting. ``None`` matches a stored null or an absent
        key.

        When the request also names a ``doc_type`` that declares a
        ``metadata_schema``, tier3 keys are validated against that
        schema's declared properties before the query runs -- a typo'd
        key raises ``Tier3SchemaViolationError`` instead of silently
        matching zero rows.
        """
        sql_filters = self._build_catalog_sql_filters(request)

        with phases.phase("query_documents"):
            # Catalog is filter-only enumeration; failed-pipeline
            # documents are visible unless the caller explicitly filters
            # them out via ``pipeline_status``.
            docs, total_count = await self._graph.query_documents(
                filters=sql_filters or None,
                limit=request.limit,
                offset=request.offset,
                sort_by=request.sort_by,
                sort_order=request.sort_order,
                default_exclude_failed=False,
            )

        hits = [
            DiscoverHit.from_summary(
                self._project_doc_summary(doc, request.response_mode),
                chunk_content=None,
                heading_path=None,
                relevance_score=None,
            )
            for doc in docs
        ]

        return DiscoverResponse(
            mode=RetrievalMode.CATALOG,
            results=hits,
            total_available=total_count,
        )

    def _build_catalog_sql_filters(self, request: DiscoverRequest) -> dict[str, object] | None:
        """Translate ``RetrievalFilters`` into the graph-store filter dict.

        Shared by document catalog enumeration and facet aggregation so
        both resolve identical filter semantics, including the tier3
        filter-key validation against the resolved doc_type's
        metadata_schema. Returns None when no filter is active.
        """
        sql_filters: dict[str, object] = {}
        if request.filters:
            if request.filters.doc_type:
                sql_filters["doc_type"] = request.filters.doc_type
            if request.filters.project:
                sql_filters["project"] = request.filters.project
            if request.filters.lifecycle_status:
                sql_filters["lifecycle_status"] = request.filters.lifecycle_status
            if request.filters.pipeline_status:
                sql_filters["pipeline_status"] = request.filters.pipeline_status
            if request.filters.tags:
                sql_filters["tags"] = request.filters.tags
            if request.filters.document_ids:
                sql_filters["document_ids"] = request.filters.document_ids
            if request.filters.source_type:
                sql_filters["source_type"] = request.filters.source_type.value
            if request.filters.tier3_metadata:
                self._validate_tier3_filter_keys(
                    request.filters.tier3_metadata, request.filters.doc_type
                )
                sql_filters["tier3_metadata"] = request.filters.tier3_metadata
        return sql_filters or None

    async def _catalog_facets(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        """Vocabulary aggregation via GROUP BY on the graph store.

        Builds the same filter dict as document catalog enumeration,
        calls ``GraphStore.query_document_facets``, and wraps each
        requested facet field's counts in a ``FacetHit`` row --
        ``facet_fields`` selects a subset (all fields when unset), in
        the fixed field order regardless of the order requested. Each
        row's values are capped to ``facet_value_limit`` entries
        (``DEFAULT_FACET_VALUE_LIMIT`` when unset) and carry the true
        distinct total, so the response is bounded regardless of vault
        size or tagging density. ``total_available`` is the count of
        documents matching the filters (the facet denominator).
        """
        sql_filters = self._build_catalog_sql_filters(request)
        requested = (
            DOCUMENT_FACET_FIELDS
            if request.facet_fields is None
            else tuple(f for f in DOCUMENT_FACET_FIELDS if f in set(request.facet_fields))
        )
        value_limit = request.facet_value_limit or DEFAULT_FACET_VALUE_LIMIT

        with phases.phase("query_document_facets"):
            facets, total_count = await self._graph.query_document_facets(
                sql_filters, fields=requested, value_limit=value_limit
            )

        hits = []
        for f in requested:
            counts = facets.get(f, FacetFieldCounts({}, 0))
            hits.append(
                FacetHit(field=f, values=counts.values, total_distinct=counts.total_distinct)
            )

        return DiscoverResponse(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            results=hits,
            total_available=total_count,
        )

    @staticmethod
    def _project_doc_summary(
        doc: Document, response_mode: ResponseMode | None
    ) -> DocumentSummary | DocumentSummaryLight:
        """Project a Document to DocumentSummary or DocumentSummaryLight.

        ``response_mode="light"`` returns the stripped variant carrying
        only id, title, doc_type, lifecycle_status, and tier3_metadata;
        every other value (including unset) returns the full
        DocumentSummary. The default-threshold rule that applies to
        edges (>5 results -> light) is intentionally NOT applied here:
        document-target callers retain full-equivalent defaults unless
        they explicitly pass ``response_mode="light"``.
        """
        if response_mode == ResponseMode.LIGHT:
            return DocumentSummaryLight.from_document(doc)
        return DocumentSummary.from_document(doc)

    # ------------------------------------------------------------------
    # Edge enumeration
    # ------------------------------------------------------------------

    async def _catalog_edges(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        """Edge enumeration via SQL filter on the edges table.

        Builds an edge-only filter dict from
        ``RetrievalFilters.{source_id, target_id, edge_type}``, calls
        ``GraphStore.query_edges``, hydrates the rows into ``EdgeHit``
        models, and applies the light/full payload selector.

        ``response_mode`` resolution: explicit value wins; if unset, falls
        back to the default-threshold rule (>5 results -> light, else
        full). Light strips every field except the identity columns
        (``edge_id``, ``source_id``, ``target_id``, ``edge_type``); full
        carries the complete envelope including anchor versions,
        rationale, native ``retracted_edge_id``, and the computed
        ``retracted_at`` / ``retracted_by_edge_id`` retraction state.
        """
        edge_filters: dict[str, object] = {}
        if request.filters is not None:
            if request.filters.source_id:
                edge_filters["source_id"] = request.filters.source_id
            if request.filters.target_id:
                edge_filters["target_id"] = request.filters.target_id
            if request.filters.edge_type:
                # EdgeType enum -> SQL string (storage table stores the
                # string value, not the Python enum object).
                edge_filters["edge_type"] = request.filters.edge_type.value

        with phases.phase("query_edges"):
            rows, total_count = await self._graph.query_edges(
                filters=edge_filters or None,
                limit=request.limit,
                offset=request.offset,
            )

        effective_mode = request.response_mode
        if effective_mode is None:
            effective_mode = (
                ResponseMode.LIGHT if total_count > LIGHT_DEFAULT_THRESHOLD else ResponseMode.FULL
            )

        hits: list[EdgeHit] = [self._hydrate_edge_hit(row, effective_mode) for row in rows]

        return DiscoverResponse(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.EDGES,
            results=hits,
            total_available=total_count,
        )

    @staticmethod
    def _hydrate_edge_hit(row: EdgeQueryRow, mode: ResponseMode) -> EdgeHit:
        """Project an EdgeQueryRow into an EdgeHit honoring the response mode.

        Light mode strips every field except the identity columns so the
        wire payload stays compact for bulk enumeration. Full mode
        carries every field defined on EdgeHit.
        """
        edge = row.edge
        if mode == ResponseMode.LIGHT:
            return EdgeHit(
                edge_id=edge.id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                edge_type=edge.edge_type,
            )
        return EdgeHit(
            edge_id=edge.id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            edge_type=edge.edge_type,
            source_valid_from_version=edge.source_valid_from_version,
            target_valid_from_version=edge.target_valid_from_version,
            rationale=edge.rationale,
            rationale_kind=(edge.rationale_kind.value if edge.rationale_kind is not None else None),
            retracted_edge_id=edge.retracted_edge_id,
            retracted_at=row.retracted_at,
            retracted_by_edge_id=row.retracted_by_edge_id,
        )

    # ------------------------------------------------------------------
    # Keyword retrieval (BH-059, BH-060, BH-061)
    # ------------------------------------------------------------------

    async def _keyword(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        """BM25-only search. No embedding required.

        A query of "*" returns all documents (useful for filter-only
        drill-downs from the dashboard).
        """
        if not request.query:
            raise MissingFieldError("query", "query is required for keyword mode")

        raw_count = 0
        searched = False
        if request.query.strip() == "*":
            with phases.phase("list_filtered"):
                hits = await self._list_filtered(request)
        else:
            fetch_limit = self._fetch_limit(request)
            with phases.phase("filter_resolution"):
                content_filters, has_doc_constraints = await self._content_filters(request)
            # Short-circuit when document-level filters resolved to zero
            # matching docs — skip the search entirely. Surface hints so
            # the caller sees the active filters that culled the result.
            if has_doc_constraints and not (content_filters and content_filters.get("document_id")):
                return DiscoverResponse(
                    mode=RetrievalMode.KEYWORD,
                    results=[],
                    total_available=0,
                    hints=self._build_hints(0, request),
                )
            with phases.phase("bm25_search"):
                results = await self._content.search_bm25(
                    request.query,
                    fetch_limit,
                    filters=content_filters,
                )
            raw_count = len(results)
            searched = True
            with phases.phase("results_to_hits"):
                hits = await self._results_to_hits(results, request)
            with phases.phase("metadata_boost"):
                hits = await self._boost_metadata_matches(hits, request)
            with phases.phase("abstract_boost"):
                hits = await self._boost_abstract_matches(hits, request)
            with phases.phase("salience_rerank"):
                hits = await self._rerank_salience(hits)

        final = hits[: request.limit]
        response = DiscoverResponse(
            mode=RetrievalMode.KEYWORD,
            results=final,
            total_available=len(hits),
            hints=self._build_hints(raw_count, request) if not final else None,
        )
        # Attached here rather than in the dispatcher: only this scope knows
        # whether a term search ran, what it returned before post-filtering,
        # and whether anything reached the response -- three facts that gate
        # the advisory.
        #
        # The third is what keeps the advisory from contradicting the results
        # beside it. Every sentence it can attach explains an empty result, and
        # the metadata boost admits documents the term search never counted --
        # it matches a title or a tag by substring in the graph store, where
        # the search matched by lexeme in the content store, and the two
        # disagree wherever a substring cuts a word. Without this the caller is
        # handed those documents together with an assertion that there are
        # none. Keyed on what the response carries rather than on the boost
        # having run, so a boosted document that scope or pipeline filtering
        # then dropped leaves an empty response still explained.
        if searched and not final:
            with phases.phase("keyword_search_warnings"):
                warnings = await self._keyword_search_warnings(request, raw_count)
            if warnings:
                self._merge_hints(response, {"warnings": warnings})
        return response

    async def _list_filtered(self, request: DiscoverRequest) -> list[DiscoverHit]:
        """Return all documents matching scope and filter criteria.

        Used for dashboard drill-downs where no content search is needed.

        Filters that map to SQL column predicates (doc_type, project,
        lifecycle_status, pipeline_status, tags, document_ids,
        tier3_metadata) are pushed into ``query_documents()``. ``scope=AUTHORITATIVE``
        remains a Python post-pass because ``authority_scope`` has no
        SQL column predicate today; the other scope values (SPECIFIC,
        FILTERED, ALL) reduce to filter or short-circuit conditions
        that the SQL predicates already cover.
        """
        filters = request.filters

        # Scope-based short-circuits before any SQL is issued.
        if request.scope == RetrievalScope.SPECIFIC:
            if not filters or not filters.document_ids:
                return []
        elif request.scope == RetrievalScope.FILTERED:
            if not filters:
                return []

        sql_filters: dict[str, object] = {}
        if filters:
            if filters.doc_type:
                sql_filters["doc_type"] = filters.doc_type
            if filters.lifecycle_status:
                sql_filters["lifecycle_status"] = filters.lifecycle_status
            if filters.project:
                sql_filters["project"] = filters.project
            if filters.pipeline_status:
                sql_filters["pipeline_status"] = filters.pipeline_status
            if filters.tags:
                sql_filters["tags"] = filters.tags
            if filters.document_ids:
                sql_filters["document_ids"] = filters.document_ids
            if filters.source_type:
                sql_filters["source_type"] = filters.source_type.value
            if filters.tier3_metadata:
                self._validate_tier3_filter_keys(filters.tier3_metadata, filters.doc_type)
                sql_filters["tier3_metadata"] = filters.tier3_metadata

        docs, _ = await self._graph.query_documents(
            filters=sql_filters or None,
            limit=10_000_000,
            offset=0,
        )

        hits: list[DiscoverHit] = []
        for doc in docs:
            # The Python failed-pipeline skip is redundant when
            # pipeline_status is unset (query_documents excludes failed
            # by default) and kept as defense-in-depth so a future change
            # to the SQL default cannot silently leak failed rows into
            # the response. The explicit-filter carve-out mirrors
            # the storage-layer pattern: when the caller passes an
            # explicit pipeline_status filter (e.g., asking for failed
            # docs), the gate steps aside and honors the override.
            if doc.pipeline_status == PipelineStatus.FAILED and (
                request.filters is None or request.filters.pipeline_status is None
            ):
                continue
            # AUTHORITATIVE is the one scope rule not expressible as a
            # SQL column predicate today. Other scope rules are already
            # satisfied by the SQL filters and the short-circuits above.
            if request.scope == RetrievalScope.AUTHORITATIVE and not doc.authority_scope:
                continue

            summary = DocumentSummary.from_document(doc)
            hits.append(
                DiscoverHit.from_summary(
                    summary,
                    chunk_content=None,
                    heading_path=None,
                    relevance_score=None,
                )
            )

        return hits

    # ------------------------------------------------------------------
    # Semantic retrieval (BH-020, BH-027, BH-028)
    # ------------------------------------------------------------------

    async def _semantic(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        if not request.query:
            raise MissingFieldError("query", "query is required for semantic mode")

        # Over-fetch when filters are active so post-filter results
        # aren't depleted by non-matching documents.
        fetch_limit = self._fetch_limit(request)

        with phases.phase("embed"):
            embeddings = await self._embedding.embed([request.query])
            query_embedding = embeddings[0]

        with phases.phase("filter_resolution"):
            content_filters, has_doc_constraints = await self._content_filters(request)
        # Short-circuit when document-level filters resolved to zero
        # matching docs — skip the search entirely. Surface hints so the
        # caller sees the active filters that culled the result.
        if has_doc_constraints and not (content_filters and content_filters.get("document_id")):
            return DiscoverResponse(
                mode=RetrievalMode.SEMANTIC,
                results=[],
                total_available=0,
                hints=self._build_hints(0, request),
            )

        if request.use_hybrid:
            with phases.phase("hybrid_rrf"):
                results = await self._hybrid_rrf(
                    query_embedding,
                    request.query,
                    fetch_limit,
                    filters=content_filters,
                )
        else:
            with phases.phase("vector_search"):
                results = await self._content.search_semantic(
                    query_embedding,
                    fetch_limit,
                    filters=content_filters,
                )

        # Filter results: exclude failed-pipeline documents (BH-020)
        raw_count = len(results)
        with phases.phase("results_to_hits"):
            hits = await self._results_to_hits(results, request)
        with phases.phase("metadata_boost"):
            hits = await self._boost_metadata_matches(hits, request)
        with phases.phase("abstract_boost"):
            hits = await self._boost_abstract_matches(hits, request)
        with phases.phase("salience_rerank"):
            hits = await self._rerank_salience(hits)

        final = hits[: request.limit]
        return DiscoverResponse(
            mode=RetrievalMode.SEMANTIC,
            results=final,
            total_available=len(hits),
            hints=self._build_hints(raw_count, request) if not final else None,
        )

    async def _hybrid_rrf(
        self,
        query_embedding: list[float],
        query_text: str,
        limit: int,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Reciprocal Rank Fusion of vector and BM25 results (BH-027).

        RRF score = sum(1 / (k + rank)) across result lists. The fusion math
        lives in ``sage.utils.rrf.rrf_fuse`` so the keyword-backend fidelity
        harness can fuse alternative keyword arms through the identical formula.
        """
        # Fetch more candidates than needed so RRF can re-rank
        fetch_limit = limit * 3

        vector_results = await self._content.search_semantic(
            query_embedding,
            fetch_limit,
            filters=filters,
        )
        bm25_results = await self._content.search_bm25(
            query_text,
            fetch_limit,
            filters=filters,
        )

        return rrf_fuse(vector_results, bm25_results, limit)

    async def _results_to_hits(
        self,
        results: list[SearchResult],
        request: DiscoverRequest,
    ) -> list[DiscoverHit]:
        """Convert SearchResults to DiscoverHits, applying pipeline and scope filters.

        Deduplicates by document ID, keeping only the highest-scoring chunk
        per document. This prevents a single document with many matching
        chunks from crowding out other documents.

        ``matched_chunk_count`` (a useful reranking signal) is the larger of
        the rows tallied here and the count the row itself carries. A binding
        that ranks chunk by chunk reports one row per chunk and leaves the
        carried count at 1, so the tally is the answer. A binding whose match
        unit is the document reports one row per document and cannot be
        tallied, so it counts its own chunks and the carried value is the
        answer. Taking the larger reads both without asking which is which.

        Only passages are tallied. A document-level row is not a passage
        (CAS-ADR-049 Decision 5), so it contributes nothing to the tally and a
        document reached through nothing else answers zero -- which is what the
        field's published description promises. Tallying it would floor every
        such document at one, including on the arm whose binding already
        reported zero correctly.
        """
        seen_docs: dict[str, DiscoverHit] = {}
        chunk_counts: dict[str, int] = {}
        carried_counts: dict[str, int] = {}
        surface_sourced: dict[str, bool] = {}
        doc_cache: dict[str, object | None] = {}
        include_content = request.response_mode != ResponseMode.LIGHT

        for result in results:
            # Count additional chunks for already-seen documents. A row can
            # arrive after the one that won the excerpt -- a document's surface
            # ranks above its own passages for a title-shaped query -- so the
            # passage test belongs on both branches, not only the first.
            if result.document_id in seen_docs:
                if not result.is_document_surface:
                    chunk_counts[result.document_id] += 1
                    # The excerpt belongs to a passage. Where the surface
                    # outranked them the held hit has none, so the document's
                    # best-ranking passage supplies it -- leaving the score
                    # alone, which is the document's best across both surfaces.
                    if surface_sourced[result.document_id]:
                        held = seen_docs[result.document_id]
                        held.chunk_content = result.content if include_content else None
                        held.heading_path = result.heading_path or None
                        surface_sourced[result.document_id] = False
                continue

            # Cache document lookups
            if result.document_id not in doc_cache:
                doc_cache[result.document_id] = await self._graph.get_document(result.document_id)
            doc = doc_cache[result.document_id]
            if doc is None:
                continue

            # BH-020: failed pipeline = excluded from all retrieval, EXCEPT when
            # the caller passes an explicit pipeline_status filter. The
            # gate mirrors the storage-layer default_exclude_failed: skip the
            # post-filter when the request asks for failed docs (or any non-default
            # pipeline_status), otherwise apply it as a service-layer mirror.
            if doc.pipeline_status == PipelineStatus.FAILED and (
                request.filters is None or request.filters.pipeline_status is None
            ):
                continue

            # Scope gating
            if not self._passes_scope(doc, request):
                continue

            summary = DocumentSummary.from_document(doc)

            # BH-084/085 + + Suppress chunk_content when
            # response_mode=light; heading_path preserved as cheap "why
            # this matched" context. When response_mode is unset, default
            # is full-equivalent (chunk content included).
            #
            # A document matched only through its document surface carries no
            # excerpt, so its heading path is empty rather than a sentinel
            # needing masking (CAS-ADR-049).
            visible_heading_path = result.heading_path or None
            hit = DiscoverHit.from_summary(
                summary,
                chunk_content=result.content if include_content else None,
                heading_path=visible_heading_path,
                relevance_score=result.score,
            )
            seen_docs[result.document_id] = hit
            chunk_counts[result.document_id] = 0 if result.is_document_surface else 1
            carried_counts[result.document_id] = result.matched_chunk_count
            surface_sourced[result.document_id] = result.is_document_surface

        # Stamp matched_chunk_count on each hit
        for doc_id, hit in seen_docs.items():
            hit.matched_chunk_count = max(chunk_counts[doc_id], carried_counts[doc_id])

        return list(seen_docs.values())

    async def _boost_metadata_matches(
        self,
        hits: list[DiscoverHit],
        request: DiscoverRequest,
    ) -> list[DiscoverHit]:
        """Boost documents matching the query by authored metadata.

        Searches the title and the tags in the graph store. Documents found by
        metadata receive a synthetic score above the highest content-search
        score. Documents already in hits have their score promoted; documents
        not yet in hits are prepended as new entries. This ensures documents
        whose identity (name, codes, tags) matches the query surface at the
        top, regardless of their content-level relevance score.

        This is a path into a caller's result set, so it is bound by the same
        provenance rule the retrieval surfaces are: derived text ranks and
        orients and never admits (CAS-ADR-049 Decision 4). What the store
        admits here is authored, which is why a filename no longer reaches a
        document by this route.
        """
        if not request.query:
            return hits

        metadata_docs = await self._graph.search_metadata(
            request.query,
            limit=100,
        )
        if not metadata_docs:
            return hits

        # Determine boost score: above the highest existing score
        max_score = max((h.relevance_score or 0.0 for h in hits), default=0.0)
        boost_base = max_score + 0.1 if max_score > 0 else 1.0

        existing_hits = {h.document.id: h for h in hits}
        boosted: list[DiscoverHit] = []

        for i, doc in enumerate(metadata_docs):
            # Service-layer mirror of the storage-layer default-exclude:
            # drop failed docs from the metadata-boost path only when the caller
            # has not asked for them. Explicit pipeline_status filters bypass.
            if doc.pipeline_status == PipelineStatus.FAILED and (
                request.filters is None or request.filters.pipeline_status is None
            ):
                continue
            if not self._passes_scope(doc, request):
                continue

            boost_score = boost_base - (i * 0.001)

            if doc.id in existing_hits:
                # Promote existing hit's score to boost level
                existing_hits[doc.id].relevance_score = boost_score
            else:
                summary = DocumentSummary.from_document(doc)
                boosted.append(
                    DiscoverHit.from_summary(
                        summary,
                        chunk_content=None,
                        heading_path=None,
                        relevance_score=boost_score,
                    )
                )

        # Re-sort existing hits so promoted ones float to the top
        hits.sort(key=lambda h: h.relevance_score or 0.0, reverse=True)
        return boosted + hits

    # ------------------------------------------------------------------
    # Abstract prefilter boost (BH-105 through BH-111, CAS-ADR-011)
    # ------------------------------------------------------------------

    # Multiplicative boost for documents whose abstract matches the query.
    _ABSTRACT_MATCH_BOOST = 1.30

    async def _boost_abstract_matches(
        self,
        hits: list[DiscoverHit],
        request: DiscoverRequest,
    ) -> list[DiscoverHit]:
        """Boost documents whose semantic_abstract matches the query.

        Implements the two-pass retrieval pattern from CAS-ADR-011:
        abstract search identifies relevant documents, then their chunk-level
        scores receive a multiplicative boost. Documents without abstracts
        or whose abstracts don't match are unaffected (not excluded).

        Disabled when use_abstract_prefilter=False on the request.
        """
        if not request.use_abstract_prefilter:
            return hits
        if not request.query:
            return hits
        if not hits:
            return hits

        # Query the graph store for documents whose abstract matches
        abstract_docs = await self._graph.search_abstracts(
            request.query,
            limit=100,
        )
        abstract_doc_ids = {doc.id for doc in abstract_docs}

        if not abstract_doc_ids:
            return hits

        # Apply multiplicative boost to matching documents
        for hit in hits:
            if hit.document.id in abstract_doc_ids and hit.relevance_score is not None:
                hit.relevance_score *= self._ABSTRACT_MATCH_BOOST

        # Re-sort by boosted score descending
        hits.sort(key=lambda h: h.relevance_score or 0.0, reverse=True)
        return hits

    # ------------------------------------------------------------------
    # Salience reranking (BH-069, BH-070)
    # ------------------------------------------------------------------

    # Recency boost: maximum multiplicative boost for very recent documents.
    # Decays exponentially with a half-life of 365 days.
    _RECENCY_MAX_BOOST = 0.10
    _RECENCY_HALF_LIFE_DAYS = 365.0

    async def _rerank_salience(
        self,
        hits: list[DiscoverHit],
    ) -> list[DiscoverHit]:
        """Apply lifecycle tier sort and recency boost.

        BH-069: Active documents always rank above non-active documents.
        Sorting is by (lifecycle_tier, score) where active = 0, other = 1.
        This guarantees the active head of a supersedes chain is always the
        top result for code-based lookups.

        BH-070: Within each tier, documents with recent dates receive an
        additional decaying multiplicative boost.

        Uses fields already present on DocumentSummary (document_date,
        source_modified_at, lifecycle_status) -- no extra graph queries.
        """
        if not hits:
            return hits

        now = datetime.now(timezone.utc)

        for hit in hits:
            if hit.relevance_score is None:
                continue

            score = hit.relevance_score

            # BH-070: Recency boost from summary fields
            ref_date = self._resolve_document_date(hit.document, now)
            if ref_date is not None:
                age_days = max((now - ref_date).total_seconds() / 86400.0, 0.0)
                decay = math.exp(-age_days * math.log(2) / self._RECENCY_HALF_LIFE_DAYS)
                score *= 1.0 + self._RECENCY_MAX_BOOST * decay

            hit.relevance_score = score

        # BH-069: Sort by (lifecycle_tier, score desc).
        # Active documents (tier 0) always rank above non-active (tier 1).
        hits.sort(
            key=lambda h: (
                0 if h.document.lifecycle_status == "active" else 1,
                -(h.relevance_score or 0.0),
            ),
        )
        return hits

    @staticmethod
    def _resolve_document_date(
        summary: DocumentSummary,
        now: datetime,
    ) -> datetime | None:
        """Pick the best available date for recency scoring.

        Priority: document_date > source_modified_at.
        Returns None if neither is available.

        ``document_date`` is a bare YYYY-MM-DD calendar-date string on
        DocumentSummary; parse it to a UTC-anchored datetime here, at
        the consumer, since the caller does date math
        (``(now - ref_date).total_seconds()``). Calendar-to-instant
        conversion belongs at the use site where the instant is needed,
        not at the projection boundary where it would re-promote a
        calendar date to a UTC instant and shift the wire-side calendar
        for non-UTC consumers.
        """
        if summary.document_date:
            return parse_document_date(summary.document_date)

        if summary.source_modified_at:
            return summary.source_modified_at

        return None

    def _passes_scope(self, doc: Document, request: DiscoverRequest) -> bool:
        """Check whether a document passes scope and filter criteria."""
        filters = request.filters

        # Scope gating
        if request.scope == RetrievalScope.AUTHORITATIVE:
            if not doc.authority_scope:
                return False
        elif request.scope == RetrievalScope.SPECIFIC:
            if filters and filters.document_ids:
                if doc.id not in filters.document_ids:
                    return False
            else:
                return False
        elif request.scope == RetrievalScope.FILTERED:
            if not filters:
                return False

        # Apply filters (for FILTERED and ALL scopes)
        if filters:
            if filters.doc_type and doc.doc_type != filters.doc_type:
                return False
            if filters.project and doc.project != filters.project:
                return False
            if filters.lifecycle_status and doc.lifecycle_status != filters.lifecycle_status:
                return False
            if filters.tags:
                if not set(filters.tags).issubset(set(doc.tags)):
                    return False
            if filters.pipeline_status:
                if doc.pipeline_status.value != filters.pipeline_status:
                    return False
            if filters.document_ids:
                if doc.id not in filters.document_ids:
                    return False
            if filters.source_type and doc.source_type != filters.source_type:
                return False

        return True

    # ------------------------------------------------------------------
    # Deterministic retrieval (BH-029, BH-030)
    # ------------------------------------------------------------------

    async def _deterministic(
        self,
        request: DiscoverRequest,
        phases: PhaseCollector | _NullPhaseCollector,
    ) -> DiscoverResponse:
        if not request.document_id:
            raise MissingFieldError("document_id", "document_id is required for deterministic mode")
        if not request.heading_path:
            raise MissingFieldError(
                "heading_path", "heading_path is required for deterministic mode"
            )

        # Validate document exists
        with phases.phase("get_document"):
            doc = await self._graph.get_document(request.document_id)
        if doc is None:
            raise DocumentNotFoundError(
                request.document_id,
                await build_not_found_detail(self._graph, request.document_id),
            )

        # Pipeline gate (BH-021)
        if doc.pipeline_status == PipelineStatus.FAILED:
            raise PipelineIncompleteError(request.document_id)

        # Prefix match on heading_path (BH-029)
        with phases.phase("get_chunks_by_heading_prefix"):
            chunks = await self._content.get_chunks_by_heading_prefix(
                request.document_id, request.heading_path
            )

        # BH-030: no matching headings = 404, with available headings for guidance
        if not chunks:
            with phases.phase("get_heading_paths"):
                available = await self._content.get_heading_paths(request.document_id)
            raise HeadingNotFoundError(request.heading_path, request.document_id, available)

        summary = DocumentSummary.from_document(doc)

        hits = [
            DiscoverHit.from_summary(
                summary,
                chunk_content=chunk.content,
                heading_path=chunk.heading_path,
                relevance_score=None,  # Deterministic mode: no relevance score
            )
            for chunk in chunks
        ]

        return DiscoverResponse(
            mode=RetrievalMode.DETERMINISTIC,
            results=hits,
            total_available=len(hits),
        )
