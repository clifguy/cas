"""Abstract base classes for swappable external dependencies.

Production implementations: Postgres (GraphStore, ContentStore),
sentence-transformers (EmbeddingProvider), MLX/Qwen3 (AbstractionProvider).
Stubs in stubs.py provide deterministic behavior for testing.

The port value types these signatures reference (``EdgeQueryRow``,
``LinkReadContext``, ``OnConflict``) live in ``sage.models.graph_rows`` so the
port depends only on the models leaf, never on a concrete store module.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple, TypedDict

from sage.models.enums import ResolutionPolicy
from sage.models.graph_rows import EdgeQueryRow, LinkReadContext, OnConflict
from sage.models.schemas import Document, Edge, LinkRequest, StagingEdge, User

# The legacy document-level marker, in both the spellings a stored row carries
# it in. It identified a per-document synthetic header row on the passage
# surface, holding the title, source filename stem, tags, semantic abstract, and
# case-split identifier tokens. Document-level text now has a surface of its own
# (CAS-ADR-049) and nothing writes either spelling.
#
# The two are written together, by one ingestion path, so a legacy row always
# carries both and neither is the canonical one. A sweep that finds one form and
# reports the class closed has looked at half the surface.
#
# This block is where the window such a row can appear in is stated, and it is
# the only place that states it. The window is open and is not expected to
# close. Every known vault having been migrated is not what would close it,
# because it does not stop another arriving unmigrated: a store restored from a
# backup taken before the decision is reprovisioned by the schema layer's
# ``IF NOT EXISTS`` bootstrap and opens without complaint, and the relocation
# runs only when an operator runs the migration, never at vault open. So an
# unmigrated vault can still arrive -- from a restore, or from a newly attached
# tenant.
# The migration surface below therefore stays, and so do the read-path guards
# that keep a legacy row from reaching a caller in the meantime: on the Postgres
# binding as a predicate scoping every passage read to indexes above
# ``LEGACY_DOCUMENT_HEADER_CHUNK_INDEX``, and on the stub binding as heading-path
# comparisons in its keyword arm. Both cite this block rather than restating it.
LEGACY_DOCUMENT_HEADER_HEADING_PATH = "__document_header__"
LEGACY_DOCUMENT_HEADER_CHUNK_INDEX = -1


class KeywordQueryParse(NamedTuple):
    """How the keyword backend parsed a query.

    ``terms`` are the lexemes a document must carry, after the backend's own
    stopword and stemming treatment and with anything the query excluded
    removed. ``excluded`` are the lexemes the query rules out; they are not
    something the caller must supply, but their presence means a search ran
    even when ``terms`` is empty -- which is what separates a query asking
    only for absences from one whose every word the backend discarded.

    ``all_required`` is false when the parse admits alternatives, so a caller
    cannot describe the query as conjunctive: a document can satisfy it while
    carrying only some of the terms. It says nothing about scope -- each
    alternative is satisfied across the document, exactly as a lone
    conjunction's terms are. ``adjacent`` is true when the parse contains a
    phrase, whose terms must appear together and in order within a single
    passage -- a stronger condition than carrying them all, so a document can
    hold every term and still not match. Adjacency is the one predicate scoped
    below the document by decision; a negation may also be, but only because
    its scope is undecided rather than settled there.
    """

    terms: tuple[str, ...]
    excluded: tuple[str, ...]
    all_required: bool
    adjacent: bool


# The spellings a plain POSIX form reduces: a `.` segment (bounded by a
# separator or an end, so a dot inside a *filename* is untouched), a doubled
# separator, and a trailing one. `..` is deliberately absent -- it is preserved
# rather than resolved, so a path carrying one is already its own plain form.
#
# Shared by every GraphStore binding's `list_non_canonical_source_paths` rather
# than written once per backend. The syntax is the intersection of POSIX and
# Python regex, so the durable store can hand it to the database and an
# in-memory store to `re`, and the two cannot answer differently.
NON_CANONICAL_SOURCE_PATH_PATTERN = r"(^|/)[.](/|$)|//|/$"


# The delimiter a heading path's elements are joined with. Shared by every
# source adapter that builds a path and by the rule that reads one back
# (CAS-ADR-049 Decision 3) rather than written once per format: a path is
# produced in one place and split in another, and the two cannot be allowed to
# answer differently. A heading whose own text contains the delimiter is
# ambiguous to every consumer that splits on it; the adapters join without
# escaping, so nothing downstream can recover the distinction.
HEADING_PATH_SEPARATOR = " > "


@dataclass
class Chunk:
    """A chunk of document content for indexing.

    ``heading_path`` is the passage's *address*: the path exactly as the source
    adapter produced it, which is what heading enumeration returns and what a
    section read accepts. ``indexed_structure`` is the passage's structure
    *relative to its document* -- that path with a root element equal to the
    document title removed, because the title is document-level and the document
    surface carries it. CAS-ADR-049 Decision 3 separates the two roles; only the
    second reaches the keyword arm's top ranking weight.

    ``indexed_structure`` is ``None`` when it has not been derived -- a vault
    that has taken the column but not yet the backfill, or a caller that built a
    chunk without a document to derive it from. A binding falls back to the
    address in that case, so an underived passage keeps the pre-decision
    behaviour rather than dropping out of the index.

    ``chunk_index`` is a passage's position in its document and is unique
    within it: a caller writing a document's passages numbers them, and passing
    the default for more than one of them is a defect in the caller. Bindings
    depend on that -- it is the term their result orderings end on, since
    nothing else distinguishes two passages of one document (a heading is not
    unique within one, and a passage may carry none). Two passages sharing an
    index re-open a tie the orderings are stated to have closed, and no schema
    constraint catches it.
    """

    document_id: str
    heading_path: str
    content: str
    embedding: list[float] | None = None
    chunk_index: int = 0
    doc_type: str | None = None
    lifecycle_status: str | None = None
    project: str | None = None
    indexed_structure: str | None = None


@dataclass
class DocumentSurface:
    """Document-level text for one document, split by provenance.

    CAS-ADR-049 carries document-level text on a retrieval surface of its own
    and makes matchability a function of provenance. The split is expressed as
    two fields rather than one blob so a binding cannot accidentally admit
    derived text to a match:

    ``matchable``
        Authored text -- the document's title and tags, together with the
        normalized renderings that let a caller reach them without reproducing
        the author's separators or word boundaries. May satisfy a match.

    ``orienting``
        Derived text -- the generated semantic abstract, the source filename
        stem, and that stem's expansion. Contributes to ranking and to
        orientation surfaces, and never satisfies a match.
    """

    document_id: str
    matchable: str
    orienting: str
    embedding: list[float] | None = None
    doc_type: str | None = None
    lifecycle_status: str | None = None
    project: str | None = None


@dataclass
class SearchResult:
    """A result from content store search.

    One row per chunk on the semantic arm. On the keyword arm the match unit
    is the document, so a row stands for a whole document: ``heading_path``
    and ``content`` carry its best-ranking chunk, and ``matched_chunk_count``
    reports how many of its chunks carry a query term. A binding that ranks
    chunk-by-chunk leaves the count at its default of 1, and the caller
    tallies duplicate ``document_id`` rows itself.

    ``is_document_surface`` names which of the two surfaces the row came from
    (CAS-ADR-049 Decision 2). A document-level row is not a passage: it carries
    no excerpt, and it is not counted by ``matched_chunk_count``, which names
    passages. The distinction is a field rather than something a caller infers,
    because neither of the two properties that look like it is one. An empty
    ``heading_path`` does not imply it -- a document with no headings has a
    genuine passage whose path is empty -- and a zero ``matched_chunk_count``
    does not survive rank fusion, which reconciles counts across arms.
    """

    document_id: str
    heading_path: str
    content: str
    score: float
    matched_chunk_count: int = 1
    is_document_surface: bool = False


@dataclass(frozen=True)
class EdgeReadFailure:
    """An edge row that failed model validation during enumeration.

    Carries the raw stored values deliberately unvalidated -- the row is being
    reported precisely because it violates the model contract -- plus the
    validation error text, so a caller can enumerate every malformed row in a
    store instead of aborting on the first one.
    """

    raw_id: str
    source_id: str | None
    target_id: str | None
    edge_type: str | None
    error: str


class ContentStoreOptimizeSnapshot(TypedDict):
    """Pre/post observations captured around ContentStore.optimize().

    Substrates with no on-disk presence (StubContentStore) return zeros.
    Postgres returns the relation's measured byte size, the retained
    dataset-version count, and the fragment counts from its bloat snapshot.
    """

    pre_bytes: int
    post_bytes: int
    pre_versions: int
    post_versions: int
    pre_fragments: int
    post_fragments: int
    pre_small_fragments: int
    post_small_fragments: int


class ContentStore(ABC):
    """Interface for vector/full-text content store (Postgres in production)."""

    @abstractmethod
    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """Store embedded chunks for a document."""

    @abstractmethod
    async def upsert_document_surface(self, surface: DocumentSurface) -> None:
        """Write a document's document-level text, replacing any prior row.

        Scoped to the document surface; the document's passages are not
        touched. Called at indexing time and again once the generated abstract
        is populated, so the derived half stays current without disturbing
        authored passages.
        """

    @abstractmethod
    async def remove_document_surface(self, document_id: str) -> None:
        """Remove a document's document-level row (idempotent)."""

    @abstractmethod
    async def update_document_surface_text(
        self, document_id: str, matchable: str, orienting: str
    ) -> bool:
        """Rewrite a document-level row's text, leaving its vector in place.

        The counterpart of ``update_chunk_metadata`` for the document surface:
        a metadata edit changes what the document says it is, so the keyword
        halves must be recomposed, but nothing about the edit warrants
        re-embedding a corpus. The stored vector therefore keeps describing the
        text as it read when the document was last indexed -- the same posture
        passages already have, where a metadata edit never re-embeds either.

        Returns whether a row was updated; false when the document has no
        document-level row.
        """

    @abstractmethod
    async def update_indexed_structure(
        self, document_id: str, derived: Sequence[tuple[str, str]]
    ) -> int:
        """Rewrite one document's derived structure, by ``(heading_path, structure)``.

        Distinct from the migration's writer, which fills only what is
        underived: a title edit changes the *correct* value for passages that
        already carry one, so this overwrites. Scoped to a single document,
        because a title belongs to one.

        Returns the number of rows written.
        """

    @abstractmethod
    async def remove_document(self, document_id: str) -> None:
        """Remove a document's passages and its document surface (idempotent).

        Used in force re-ingestion. Both surfaces are cleared, so a re-ingest
        cannot leave a stale document-level row behind a rebuilt passage set.
        """

    # -- migration off the single-surface layout (CAS-ADR-049) ---------------
    # A vault provisioned before document-level text had a surface of its own
    # still holds a synthetic header row per document on the passage surface.
    # These two exist to relocate that text, and have no caller outside the
    # migration. They are permanent members of this contract rather than a
    # one-shot awaiting removal: the condition that would retire them is stated
    # at LEGACY_DOCUMENT_HEADER_HEADING_PATH, and it is not met.

    @abstractmethod
    async def legacy_document_header_rows(self) -> list[tuple[str, list[float] | None]]:
        """Return ``(document_id, embedding)`` for each legacy header row.

        The embedding is carried forward to the relocated document-level row
        so a migration need not re-embed the corpus.
        """

    @abstractmethod
    async def delete_legacy_document_header_rows(self) -> int:
        """Delete every legacy header row; returns the number removed."""

    # -- migration to the relative indexed structure (CAS-ADR-049 Decision 3) --
    # A vault provisioned before a passage's structure was separated from its
    # address indexes the whole heading path at the top ranking weight, and
    # carries no derived structure at all. These three repair that, and have no
    # caller outside the migration.

    @abstractmethod
    async def passages_awaiting_indexed_structure(self) -> list[tuple[str, str]]:
        """Return the distinct ``(document_id, heading_path)`` still underived.

        Distinct rather than per-row because the derived value is a function of
        exactly that pair, so one derivation serves every passage sharing it.
        An empty result means the backfill has nothing to do -- which, with the
        vector rebuild reported separately, is not on its own enough to
        conclude that the vault is migrated.
        """

    @abstractmethod
    async def passage_vector_ranks_indexed_structure(self) -> bool:
        """Whether the keyword arm already ranks the relative structure.

        Reported separately from whether any passage still awaits derivation,
        because the two can disagree: a migration interrupted after its backfill
        leaves nothing to derive and a vector still built from the address, so a
        caller guarding only on the backfill would never repair it.
        """

    @abstractmethod
    async def migrate_indexed_structure(self, derived: Sequence[tuple[str, str, str]]) -> int:
        """Apply ``(document_id, heading_path, structure)`` and repair the vector.

        Keyed on the pair rather than on a row identity because the passage
        surface carries no primary key, and because the value is a function of
        exactly that pair -- so applying it to every row sharing it is correct
        rather than merely convenient. A row that already carries a derived
        value is left alone, so a re-run cannot overwrite what a later ingest
        wrote from a newer title.

        Atomic, and it also brings the keyword vector to the relative structure
        if it is not there yet -- one operation because a binding may be able to
        order the two so that the cheaper one runs under the other's cover.

        Returns the number of rows the derivation wrote.
        """

    @abstractmethod
    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search.

        filters: optional pre-filter predicates (e.g. {"doc_type": "design_spec"}).
        Values may be a single string (equality) or a list of strings
        (IN clause). When provided, only chunks matching all predicates
        are searched.
        """

    @abstractmethod
    async def parse_keyword_query(self, query: str) -> KeywordQueryParse:
        """How ``search_bm25`` read this query, so a caller can be told why it missed.

        The raw query text cannot answer that on the production binding, which
        drops stopwords and stems the rest, so the required terms are neither
        the words typed nor a whitespace split of them. Terms the query
        excludes are omitted, and a query admitting alternatives reports
        ``all_required=False`` -- describing such a query as conjunctive would
        state the opposite of what the caller wrote.

        Carries no terms for a blank query, matching ``search_bm25``. A
        non-blank query can also carry none, when every word in it is a
        stopword: the backend then searched for nothing at all.
        """

    @abstractmethod
    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Keyword search. The match unit is the document (CAS-ADR-048).

        A document matches when the union of its chunks carries every required
        term; the terms need not co-occur in any one of them. Scoping the
        conjunction to a chunk instead would make retrieval a function of
        projection -- a change of chunking strategy would alter which queries
        match, with no change to the corpus or the query -- and would scope the
        match to a unit the caller cannot see or predict, while the result this
        returns is document-shaped. ``parse_keyword_query`` reports which terms
        the backend actually required, which the raw query text cannot.

        The chunk remains the ranking and excerpt unit: a matching document is
        returned once, ranked by its best-matching chunk and carrying that
        chunk's ``heading_path`` and ``content``. Co-occurrence within a single
        chunk is a ranking signal, not a matching precondition. ``limit`` is
        therefore a document budget, and ``matched_chunk_count`` reports how
        many of the document's chunks carry a query term.

        A quoted phrase is the one exception: adjacency across a chunk boundary
        is not meaningful, so a phrase must be satisfied within a single chunk.
        A query may mix the two -- its phrases chunk-scoped, its bare terms not.

        Only authored text satisfies a match (CAS-ADR-049). Machine-generated
        and incidental text -- a generated abstract, a source filename, a
        lexical identifier expansion -- contributes to ranking and orientation,
        but a term appearing only there does not make a document match.

        How the union is computed is a binding concern the contract does not
        constrain: an aggregate index, a per-term intersection, and a two-pass
        resolution are all admissible. A binding may also decline a query whose
        form it cannot express at document scope and evaluate it against a
        single chunk instead, which changes what has to satisfy the query and
        so what ``matched_chunk_count`` counts -- the chunks satisfying it
        entire rather than those carrying a required term.

        What that does not change is the row shape. A matching document is
        returned once whatever answered it, and ``limit`` is a document budget
        on every keyword answer. A binding spending it on rows let one
        document's chunks fill the budget and returned that document alone,
        which a caller cannot distinguish from a corpus holding nothing else.

        filters: optional pre-filter predicates (e.g. {"doc_type": "design_spec"}).
        Values may be a single string (equality) or a list of strings
        (IN clause). Predicates apply at the matching unit: they select a slice
        of each document's chunks and the union is computed inside that slice,
        rather than over every chunk and filtered afterwards. A filter only
        narrows -- it admits no document the equivalent unfiltered search
        excludes -- and computing the union inside the slice is what keeps the
        two consistent, since a document whose terms are spread across chunks
        the filter does not select no longer carries them all.
        """

    @abstractmethod
    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata columns on all chunks for a document.

        Used to sync content-store metadata when document metadata changes
        (e.g. doc_type reassignment via update_metadata).
        """

    @abstractmethod
    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path starts with the given prefix.

        Used by deterministic retrieval mode. Returns chunks in document
        order (by chunk_index).
        """

    @abstractmethod
    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths for a document in document order.

        Used to populate available_headings in HeadingNotFoundError
        so callers can see what headings actually exist.
        """

    @abstractmethod
    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document.

        Lightweight existence check without loading chunk content.
        Used by reabstract for synchronous validation before
        dispatching background work.
        """

    @abstractmethod
    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order.

        Used by export_projection to reconstruct the projection text.
        """

    @abstractmethod
    async def count_chunks(self) -> int:
        """Return the total number of chunk rows across all documents.

        Returns 0 when the underlying table has not been created yet.
        """

    @abstractmethod
    async def count_retained_versions(self) -> int:
        """Return the number of retained dataset versions in the store.

        Read-only. Rises monotonically with un-optimized write churn and is
        independent of corpus size, so it is the self-calibrating signal
        behind the dashboard bloat indicator. Returns 0 for substrates with
        no on-disk versioning and when the underlying table has not been
        created yet.
        """

    @abstractmethod
    async def count_small_fragments(self) -> int:
        """Return the number of small (un-compacted) fragments in the store.

        Read-only. Small fragments accumulate with un-optimized write churn
        and are merged away by ``optimize``; unlike a total fragment count,
        a healthy store keeps this near zero regardless of corpus size, so it
        is a self-calibrating bloat signal alongside the retained-version
        count. Returns 0 for substrates with no on-disk fragments and when
        the underlying table has not been created yet.
        """

    @abstractmethod
    async def measured_byte_size(self) -> int:
        """Return the content store's on-disk byte footprint.

        Read-only. The substrate-native total size of the chunk store
        (e.g. a directory byte sum for a file-backed store, or the total
        relation size for a relational one), so the dashboard can report
        content-store size without knowing which binding is active.
        Returns 0 for substrates with no on-disk presence and when the
        underlying store has not been created yet.
        """

    @abstractmethod
    async def optimize(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        """Reclaim disk by compacting fragments and pruning old versions.

        Snapshots substrate state immediately before and after the
        reclamation call; returns the pair so callers can observe
        what changed. Substrates with no on-disk presence
        (StubContentStore) return zeros; Postgres returns the relation's
        measured byte size, version count, and fragment counts.

        cleanup_older_than: how old a retained dataset version must be
        to be eligible for pruning. Postgres has no age-threshold analog
        (VACUUM reclaims every eligible dead tuple) and ignores this
        parameter; it is preserved for substrates that do support pruning
        by age. The service-layer default is 7 days.
        """


class EmbeddingProvider(ABC):
    """Interface for text embedding (sentence-transformers in production)."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""


class AbstractionProvider(ABC):
    """Interface for semantic abstract generation (MLX/Qwen3 in production)."""

    @abstractmethod
    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Generate a density-proportional semantic abstract.

        doc_type is surfaced to the model so it can pick the right
        descriptive verbs (prescribes, argues, narrates, defines) and
        avoid restating identifying metadata the agent already sees.
        Pass None when no doc_type is available.
        """


class NaturalKeyConflict(Exception):
    """Storage-layer signal that an edge or staging-edge natural-key triple
    ``(source_id, target_id, edge_type)`` already exists.

    Backend-neutral by design: every concrete store translates its driver's
    unique-violation (Postgres's ``UniqueViolation`` on the natural-key index)
    into this one type, so callers above the port never branch on the driver.
    Raised at every write escape point where a natural-key duplicate surfaces
    under ``on_conflict="raise"``; the ``on_conflict="noop"`` path resolves
    the duplicate internally and never raises this.
    """

    def __init__(self, source_id: str, target_id: str | None, edge_type: str) -> None:
        super().__init__(
            f"edge natural key (source={source_id!r}, target={target_id!r}, "
            f"type={edge_type!r}) already exists"
        )
        self.source_id = source_id
        self.target_id = target_id
        self.edge_type = edge_type


class StorageQueryError(Exception):
    """Storage-layer signal that the backend refused a document query.

    Backend-neutral by design, like ``NaturalKeyConflict``: a concrete
    store translates its driver's query failure into this one type, so
    callers above the port never branch on the driver.

    A driver's own message quotes the failing statement and any backend
    hint. That text is diagnostic for an operator and must not reach a
    caller, so it travels here in ``driver_message`` for server-side
    logging while the service layer raises the public, curated
    ``StorageQueryFailedError`` in its place.
    """

    def __init__(self, operation: str, driver_message: str) -> None:
        super().__init__(f"storage query {operation!r} failed")
        self.operation = operation
        self.driver_message = driver_message


class NonRetryableAbstractionError(Exception):
    """Abstraction failure that is deterministic in its input.

    Provider-neutral by design: the retry budget above the port is spent only
    on failures a later attempt could resolve, so an abstraction provider
    raises this type when repeating the call would reproduce the same outcome
    identically. Callers classify on this base class and never on a specific
    provider's SDK exception types, which keeps the layer above the port free
    of any knowledge of which provider is serving abstraction.
    """


class AbstractionInputTooLargeError(NonRetryableAbstractionError):
    """Document text exceeds the input budget of the configured model.

    Carries the measured input size and the budget it overran so the recorded
    failure is actionable without re-deriving either. ``input_tokens`` is None
    when the overrun was reported by the provider's API rather than counted
    before the call, in which case only the model is known with certainty.
    """

    def __init__(self, model_id: str, input_tokens: int | None, budget_tokens: int | None) -> None:
        measured = f"{input_tokens} tokens" if input_tokens is not None else "input"
        against = (
            f"the {budget_tokens}-token input budget"
            if budget_tokens is not None
            else "the input budget"
        )
        super().__init__(
            f"abstraction input too large: {measured} exceeds {against} for model {model_id!r}"
        )
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.budget_tokens = budget_tokens


class AbstractionMemoryExhaustedError(NonRetryableAbstractionError):
    """Accelerator memory was exhausted while generating an abstract.

    Raised by a provider whose model runs in local accelerator memory when an
    allocation fails mid-inference. The failure is deterministic in the input
    at the configured window: the document, the model, and the memory budget
    are the same on every attempt, so a retry re-pays the full prefill only to
    hit the same allocation failure. Distinct from a preflight free-memory
    check, which costs nothing to repeat and may clear as other load subsides.
    """

    def __init__(self, model_id: str, input_chars: int) -> None:
        super().__init__(
            f"abstraction generation exhausted accelerator memory for model "
            f"{model_id!r} on {input_chars} chars of input"
        )
        self.model_id = model_id
        self.input_chars = input_chars


# The fixed set of document metadata fields exposed as facets by
# ``GraphStore.query_document_facets``. Scalar columns aggregate directly;
# ``tags`` aggregates over the normalized per-document tag rows.
DOCUMENT_FACET_FIELDS: tuple[str, ...] = (
    "doc_type",
    "lifecycle_status",
    "source_type",
    "pipeline_status",
    "tags",
)


class FacetFieldCounts(NamedTuple):
    """One facet field's aggregation: its (possibly capped) value counts
    plus the true distinct-value total for the filter slice.

    ``total_distinct`` is computed before any value cap is applied, so
    truncation is always detectable as ``len(values) < total_distinct``.
    """

    values: dict[str, int]
    total_distinct: int


class GraphStore(ABC):
    """Interface for the document/edge/user graph store (Postgres in production).

    Captures the surface the service layer consumes. Backend-specific
    introspection that has no cross-store meaning is intentionally omitted
    from the port and lives only on the concrete impl.
    """

    # --- Lifecycle ---
    @abstractmethod
    async def initialize(self, migrate: bool = False) -> None:
        """Prepare the store for use; apply pending migrations when migrate=True."""

    @abstractmethod
    async def close(self) -> None:
        """Release all backing resources. Idempotent; subsequent ops raise."""

    async def storage_present(self, vault_id: str) -> bool:
        """Whether the vault's durable backing still exists out of band.

        Registry reconciliation consults this before trusting query results:
        a backing removed outside the process (an out-of-band vault teardown)
        must read as absent even when the store's queries would still resolve
        somewhere and appear to succeed. Defaulted, not abstract: for a backend
        whose backing cannot vanish independently of the open store handle,
        presence is implied and this default answers True.
        """
        return True

    # --- Documents ---
    @abstractmethod
    async def insert_document(self, doc: Document) -> None:
        """Persist a new document record."""

    @abstractmethod
    async def get_document(self, doc_id: str) -> Document | None:
        """Return the document with this id, or None if absent."""

    @abstractmethod
    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        """Apply a partial update; return the updated document or None if absent."""

    @abstractmethod
    async def list_all_documents(self) -> list[Document]:
        """Return every document record in the store."""

    @abstractmethod
    async def query_documents(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str | None = None,
        sort_order: str | None = None,
        *,
        default_exclude_failed: bool = True,
    ) -> tuple[list[Document], int]:
        """Filtered, paginated, optionally-sorted document query.

        Returns the page plus the total count of matching rows. By default
        excludes documents whose pipeline ended in a failed state.

        The ordering is total whatever ``sort_by`` asks for: documents tie
        freely on every sortable column, so an implementation must break the
        remaining ties on something unique per row. Two consequences callers
        rely on. Paging one filtered set with ``limit``/``offset`` returns each
        matching document exactly once -- no skips, no duplicates across a page
        boundary. And two calls with the same filters agree on row order, which
        is what lets a caller reason about a prefix of a larger page.
        """

    @abstractmethod
    async def query_document_facets(
        self,
        filters: dict[str, object] | None = None,
        *,
        fields: Sequence[str] | None = None,
        value_limit: int | None = None,
    ) -> tuple[dict[str, FacetFieldCounts], int]:
        """Distinct-value counts per facet field within a filter slice.

        Returns a mapping keyed by the requested ``fields`` (every
        ``DOCUMENT_FACET_FIELDS`` entry when None) -- each value a
        ``FacetFieldCounts`` whose ``values`` maps distinct non-null
        field values to matching-document counts, ordered by descending
        count then value, and whose ``total_distinct`` is the true
        distinct-value count for the slice -- plus the total count of
        documents matching the filters. ``value_limit`` caps each
        field's ``values`` to the top entries in that ordering (None =
        uncapped); ``total_distinct`` is unaffected by the cap.
        Unrequested fields are not aggregated. Fields with no matching
        values map to ``({}, 0)``. Applies no default failed-pipeline
        exclusion, matching catalog enumeration.
        """

    @abstractmethod
    async def find_documents_by_title(self, title: str) -> list[Document]:
        """Return documents whose title matches exactly."""

    @abstractmethod
    async def search_metadata(
        self,
        query: str,
        limit: int = 20,
        filters: dict[str, object] | None = None,
    ) -> list[Document]:
        """Keyword search over a document's authored metadata.

        The title and the tags are authored and admit a document. The source
        path is derived -- incidental to how the document arrived -- so it may
        order the admitted but never widen them (CAS-ADR-049 Decision 4).

        Ordered by match quality first -- a title match above a source-path
        match -- and then, among matches of equal quality, by salience: active
        documents first, then the most recent, ties broken on the primary key.
        A document's date for that purpose is its authored ``document_date``,
        falling back to the date it was last modified; one with neither sorts
        last. Callers truncate this result, so the ordering decides which
        matches survive the cut, and these terms are what keep that cut
        reproducible and stop it dropping a document the caller's own ranking
        would have raised.

        **Filtering happens here, ahead of the cut.** ``filters`` takes the
        same shape ``query_documents`` accepts and resolves with the same
        semantics. A caller must not narrow this result itself and expect the
        survivors to be its own best matches: what the ordering above ranks is
        whatever the ``WHERE`` clause admitted, so the cut a caller receives is
        the best of the documents *it is eligible for* only if its constraints
        were supplied here. Applying them afterwards ranks over the whole
        corpus and can return a full cut of documents the caller then discards
        entirely.

        Failed-pipeline documents are excluded unless ``filters`` names a
        ``pipeline_status`` -- the rule ``query_documents`` follows, inherited
        rather than separately switchable, because a boost has no use for a
        failed document its caller did not ask for. ``has_authority_scope`` is
        the one key here that is not also an equality: it admits documents
        whose ``authority_scope`` is set and non-empty, which is what a caller
        scoped to authoritative documents can use.
        """

    @abstractmethod
    async def search_abstracts(
        self,
        query: str,
        limit: int = 20,
        filters: dict[str, object] | None = None,
    ) -> list[Document]:
        """Keyword search over generated semantic abstracts.

        Ordered by the same salience terms the sibling above applies beneath
        its match-quality keys, and for the same reason, but with nothing ahead
        of them. Containment in an abstract admits a document and says nothing
        about how well it matched, so there is no quality signal to rank the
        set by first.

        ``filters`` means what it means above, and for the same reason: the
        cut is drawn from the documents the ``WHERE`` clause admitted, so a
        caller's constraints belong here rather than on the result. The
        failed-pipeline default is the same one too.
        """

    # --- Tier3 unique indexes ---
    @abstractmethod
    async def ensure_tier3_unique_index(self, doc_type: str, field: str) -> None:
        """Create the partial unique index enforcing a tier3 field's uniqueness."""

    @abstractmethod
    async def drop_tier3_unique_index(self, doc_type: str, field: str) -> None:
        """Drop the tier3 unique index for a (doc_type, field) pair."""

    @abstractmethod
    async def tier3_unique_index_exists(self, doc_type: str, field: str) -> bool:
        """Return True if the tier3 unique index for this pair exists."""

    @abstractmethod
    async def find_chain_heads_with_tier3_value(
        self, doc_type: str, field: str
    ) -> list[tuple[object, list[str]]]:
        """Group chain-head documents by their tier3 field value.

        Returns (value, [head_ids]) pairs so callers can detect collisions
        before enabling a uniqueness constraint.
        """

    # --- Edges ---
    @abstractmethod
    async def insert_edge(self, edge: Edge, on_conflict: OnConflict = "raise") -> tuple[Edge, bool]:
        """Insert an edge; return (edge, created). on_conflict picks raise vs no-op."""

    @abstractmethod
    async def find_edge_by_natural_key(
        self, source_id: str, target_id: str | None, edge_type: str
    ) -> Edge | None:
        """Look up an edge by its (source, target, type) natural key."""

    @abstractmethod
    async def supersede_atomic(
        self, predecessor_id: str, predecessor_updates: dict, edge: Edge
    ) -> Document | None:
        """Atomically mark a predecessor superseded and insert the supersedes edge."""

    @abstractmethod
    async def insert_with_supersede_atomic(
        self,
        new_doc: Document,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> tuple[Document, Document]:
        """Atomically insert a new document and supersede its predecessor."""

    @abstractmethod
    async def get_edges_by_source(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        """Return edges originating at a source, optionally filtered by type."""

    async def get_edges_by_source_with_failures(
        self, source_id: str
    ) -> tuple[list[Edge], list[EdgeReadFailure]]:
        """Return a source's outbound edges plus per-row validation failures.

        Default: delegate to :meth:`get_edges_by_source` and report no
        failures, which is correct for stores whose rows were all validated at
        insert time. A store that can hold pre-validation history overrides
        this to convert each row independently, so one malformed row is
        reported as an :class:`EdgeReadFailure` instead of aborting the whole
        enumeration.
        """
        return await self.get_edges_by_source(source_id), []

    @abstractmethod
    async def get_edges_by_target(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        """Return edges pointing at a target, optionally filtered by type."""

    @abstractmethod
    async def query_edges(
        self,
        *,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[EdgeQueryRow], int]:
        """Filtered, paginated edge enumeration with computed retraction state."""

    @abstractmethod
    async def get_supersedes_lineage(self, doc_id: str) -> list[str]:
        """Return the ordered supersedes lineage ids reachable from a document."""

    @abstractmethod
    async def has_supersedes_successor(self, doc_id: str) -> bool:
        """Return True if any edge supersedes this document."""

    @abstractmethod
    async def has_supersedes_predecessor(self, doc_id: str) -> bool:
        """Return True if this document supersedes another."""

    @abstractmethod
    async def find_tombstone_candidates(self, lineage_ids: list[str]) -> list[str]:
        """Return lineage ids eligible for tombstoning during a merge."""

    @abstractmethod
    async def merge_atomic(
        self,
        merged_from_edge: Edge,
        tombstone_edge_ids: list[str],
        tombstone_version: str,
    ) -> None:
        """Atomically record a merge: insert the merged_from edge and tombstones."""

    @abstractmethod
    async def read_link_context(
        self, request: LinkRequest, policy: ResolutionPolicy
    ) -> LinkReadContext:
        """Pre-fetch the state needed to validate and execute a link request."""

    @abstractmethod
    async def get_retracts_for_edges(self, edge_ids: list[str]) -> dict[str, list[Edge]]:
        """Map each edge id to the retracts edges that disclaim it."""

    @abstractmethod
    async def get_edge(self, edge_id: str) -> Edge | None:
        """Return the edge with this id, or None if absent."""

    @abstractmethod
    async def delete_edge(self, edge_id: str) -> bool:
        """Delete an edge by id; return True if a row was removed."""

    @abstractmethod
    async def find_documents_by_hashes(self, hashes: list[str]) -> dict[str, str]:
        """Map source content hashes to the document ids that carry them."""

    @abstractmethod
    async def find_documents_by_source_paths(self, source_paths: list[str]) -> dict[str, str]:
        """Map source paths to the content hash of one document carrying each.

        Paths no document carries are absent from the mapping. Several
        documents may share a source path (re-ingest, supersession); the
        lowest-ordering document id represents the path, so the answer does
        not depend on storage order. No lifecycle filtering is applied.
        """

    @abstractmethod
    async def find_document_ids_by_source_paths(
        self, source_paths: list[str]
    ) -> dict[str, list[str]]:
        """Map source paths to the ids of *every* document carrying each.

        The companion of :meth:`find_documents_by_source_paths`, which answers
        a different question: that one collapses the several-documents-one-path
        case to a single representative, because provenance needs one answer.
        A caller asking who else holds a path needs all of them, so nothing is
        collapsed here. Ids are ordered within a path so the answer does not
        depend on storage order; paths no document carries are absent from the
        mapping. No lifecycle filtering is applied.
        """

    @abstractmethod
    async def list_non_canonical_source_paths(self) -> dict[str, str]:
        """Map document id to stored source_path, for paths not in plain form.

        A record is selected when its stored path carries something a plain
        POSIX form reduces -- a ``.`` segment, a doubled separator, or a
        trailing one. Each is mapped to the value the record holds, not to its
        plain form: deciding what that form is, and whether the path has one at
        all, belongs to the caller.

        Every binding answers with :data:`NON_CANONICAL_SOURCE_PATH_PATTERN`,
        so the question is asked once rather than approximated per backend.

        The selection is a **superset**, not an exact answer: it is syntactic,
        while whether a plain form actually differs is a question about the
        path reducer. A few spellings match the pattern and still reduce to
        themselves -- ``.``, ``/``, and anything under a ``//`` root, which
        POSIX leaves implementation-defined and the reducer preserves. A caller
        that reduces each candidate and compares finds nothing to do for those.
        Settling it there is deliberate: a predicate exact enough to exclude
        them would have to re-encode the reducer's own treatment of ``//``, and
        a second encoding of that rule is the drift this method exists to
        avoid.

        ``..`` is not a selector. It is preserved rather than resolved, so such
        a path is already the only spelling of itself and there is nothing here
        to reduce. No lifecycle filtering is applied.
        """

    # --- Out-of-band removal / selection ---
    # These exist to support out-of-band operator purge tooling. Document
    # removal is absent from the SAGE request surface by the No-Delete
    # Invariant (CAS-ADR-029); it lives behind the port for maintenance use.
    @abstractmethod
    async def remove_document(self, document_id: str) -> None:
        """Delete a document's entire graph footprint in one transaction.

        Removes the ``documents`` row together with its tags and every edge
        and staging edge that references it at either end. Coordination is
        internal to this store only; no cross-store atomicity is implied
        (CAS-ADR-042 weakest-binding), so the content store is removed in a
        separate call. Absent target is a no-op.
        """

    @abstractmethod
    async def find_documents_ingested_between(
        self, since: datetime, until: datetime | None = None
    ) -> list[Document]:
        """Return documents whose ingest time falls in a half-open window.

        Selects on ``created_at`` (ingest time): lower bound inclusive, upper
        bound exclusive (``since <= created_at < until``). ``until=None``
        leaves the window open at the top. Results are ordered by
        ``created_at`` ascending.
        """

    # --- Staging edges ---
    @abstractmethod
    async def list_staging_edges(self) -> list[StagingEdge]:
        """Return all pending staging edges."""

    @abstractmethod
    async def get_staging_edge(self, edge_id: str) -> StagingEdge | None:
        """Return the staging edge with this id, or None if absent."""

    @abstractmethod
    async def insert_staging_edge(
        self, edge: StagingEdge, on_conflict: OnConflict = "raise"
    ) -> tuple[StagingEdge, bool]:
        """Insert a staging edge; return (edge, created)."""

    @abstractmethod
    async def delete_staging_edge(self, edge_id: str) -> bool:
        """Delete a staging edge by id; return True if a row was removed."""

    @abstractmethod
    async def count_staging_edges(self) -> int:
        """Return the number of pending staging edges."""

    # --- Statistics ---
    @abstractmethod
    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        """Return document counts grouped by a metadata field's value."""

    @abstractmethod
    async def get_edge_counts_by_type(self) -> dict[str, int]:
        """Return edge counts grouped by edge type."""

    @abstractmethod
    async def get_total_document_count(self) -> int:
        """Return the total number of documents."""

    @abstractmethod
    async def get_total_edge_count(self) -> int:
        """Return the total number of edges."""

    @abstractmethod
    async def get_last_ingestion_at(self) -> datetime | None:
        """Return the most recent ingestion timestamp, or None if empty."""

    @abstractmethod
    async def count_documents_by_pipeline_status(self, status: str) -> int:
        """Return the number of documents in a given pipeline status."""

    @abstractmethod
    async def clear_pipeline_error_for_statuses(self, statuses: list[str]) -> int:
        """Null pipeline_error on documents whose pipeline_status is in ``statuses``.

        Only rows that actually carry a non-null pipeline_error are touched;
        the return value is the number of rows changed, so a caller can report
        the repair only when there was something to repair. Idempotent: a
        second call over the same statuses returns 0.
        """

    @abstractmethod
    async def list_pending_metadata_documents(self) -> list[Document]:
        """Return documents awaiting metadata confirmation."""

    @abstractmethod
    async def measured_byte_size(self) -> int:
        """Return the graph store's on-disk byte footprint.

        Read-only. The substrate-native total size of the document/edge/user
        tables (e.g. a file-size sum for a file-backed store, or the total
        relation size for a relational one), so the dashboard can report
        graph-store size without knowing which binding is active. Returns 0
        for substrates with no on-disk presence and when the underlying
        tables have not been created yet.
        """

    # --- Traversal ---
    @abstractmethod
    async def traverse(
        self, start_id: str, edge_type: str | None, direction: str, depth: int
    ) -> list[dict]:
        """Walk the edge graph from a start document with chain-scoped resolution."""

    @abstractmethod
    async def chain_walk(self, start_id: str, edge_type: str) -> list[dict]:
        """Walk an edge chain to both ends, returning ordered positional metadata."""

    @abstractmethod
    async def list_provenance_edges(self, edge_types: list[str]) -> list[dict]:
        """Return provenance edges of the requested types for integrity checks."""

    @abstractmethod
    async def head_with_hash_for_chain(self, target_id: str, edge_type: str = "supersedes") -> dict:
        """Return the chain head id and its source content hash for a target."""

    # --- Users ---
    @abstractmethod
    async def insert_user(self, user: User) -> None:
        """Persist a new user record."""

    @abstractmethod
    async def get_user(self, user_id: str) -> User | None:
        """Return the user with this id, or None if absent."""

    @abstractmethod
    async def get_user_by_display_name(self, display_name: str) -> User | None:
        """Return the user with this display name, or None if absent."""

    @abstractmethod
    async def list_users(self) -> list[User]:
        """Return all user records."""
