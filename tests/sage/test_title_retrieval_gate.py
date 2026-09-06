"""A document must be reachable by its own title, however the title is typed.

CAS-ADR-049 moves a document's title off every passage's structural field and
onto a retrieval surface of its own. It records that ranking changes as a
result, and does not bound how. This module supplies the bound: a title query
returns its own document first, and does so for the separator and case variants
a caller might plausibly type rather than only for the title reproduced
verbatim.

Every assertion here runs against a real backend. The in-memory double models
neither the two-surface union nor the provenance bar, so a green run against it
would be evidence about the double and not about any binding.

The corpus is deliberately crowded: twelve documents sharing a working
vocabulary, so ranking first is a result rather than an artifact of being the
only candidate. ``test_corpus_is_actually_competitive`` fails if that stops
being true.

Every passage is addressed the way a markdown source addresses one -- rooted at
the document's title, because its H1 *is* its title -- and carries the indexed
structure CAS-ADR-049 Decision 3 derives from that address. So the gate runs
over the shape the decision changes rather than over a synthetic ``Body`` path
that never exercises it.

What the gate does *not* try to show is a rank-1 improvement from that
derivation, and the reason is worth recording. Whatever leaves a passage's
indexed structure is, by the rule's construction, exactly the document title
its own document surface already carries at the same ranking weight -- so on a
corpus this size the derivation moves score magnitudes and never the order.
The ordering effect is a corpus-scale phenomenon: it needs a document whose many
passages out-score a rival's surface row. Measuring it belongs to the
reference-corpus sweep, not to a gate; what a gate can hold is that Decision 8
still binds, which is what the rank-1 assertions below do.
"""

from datetime import datetime, timezone

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import HEADING_PATH_SEPARATOR, Chunk, DocumentSurface
from sage.models.enums import SourceType
from sage.models.schemas import Document
from sage.services.document_surface import compose_document_surface
from sage.services.passage_structure import indexed_structure
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(pg_pool):
    """The Postgres binding over a truncated, schema-isolated test vault."""
    return PostgresContentStore(pg_pool)


# Titles chosen for the shapes that break naive tokenization, over a shared
# vocabulary so every query has real competition:
#   - a PREFIX-NNN identifier, which the parser reads as a signed integer
#   - a camelCase compound, which the parser leaves as one opaque lexeme
#   - hyphenated compounds
#   - plain spaced titles that overlap the others' words
_CORPUS: list[tuple[str, str, str]] = [
    # (document_id, title, body prose)
    (
        "00000001_adr_001",
        "ADR-001: LangGraph orchestration engine",
        "orchestration engine prose",
    ),
    ("00000002_adr_002", "ADR-002: Retrieval surface boundaries", "retrieval surface prose"),
    (
        "00000049_adr_049",
        "ADR-049: Document-level text is a separate retrieval surface",
        "surface prose",
    ),
    ("0000000a_camel", "documentLevelText normalization", "normalization prose"),
    ("0000000b_spaced", "Document Level Text Handling", "handling prose"),
    ("0000000c_dash", "Document-Level Retrieval Boundaries", "boundaries prose"),
    ("0000000d_orch", "Orchestration Engine Notes", "orchestration engine notes prose"),
    ("0000000e_retr", "Retrieval Engine Notes", "retrieval engine notes prose"),
    ("0000000f_surf", "Surface Boundaries Digest", "surface boundaries digest prose"),
    ("00000010_norm", "Normalization Digest", "normalization digest prose"),
    ("00000011_text", "Text Handling Digest", "text handling digest prose"),
    ("00000012_misc", "Miscellaneous Engine Surface Text", "miscellaneous prose"),
]


def _document(document_id: str, title: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{document_id}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="adr",
        project="CAS",
        tags=["retrieval"],
        semantic_abstract=f"Generated summary of {title}.",
    )


def _distinct_embedding(index: int, *, surface: bool) -> list[float]:
    """A unit vector unique to one seeded row.

    Distinguishable rather than uniform so the semantic assertion can name
    which row it retrieved. A shared constant vector would make every seeded
    embedding interchangeable, and the setup would carry no weight.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[(index * 2) + (1 if surface else 0)] = 1.0
    return vector


def _passage(index: int, document_id: str, title: str, body: str, *, derived: bool) -> Chunk:
    """One passage, addressed the way the markdown adapter addresses one.

    The address is rooted at the title, because that is what a source whose H1
    is its title produces -- the shape most of the corpus is made of, and the
    one the indexed structure is defined against. ``derived`` selects between a
    vault that has taken the migration and one that has not.
    """
    address = f"{title}{HEADING_PATH_SEPARATOR}Body"
    return Chunk(
        document_id=document_id,
        heading_path=address,
        indexed_structure=indexed_structure(address, title) if derived else None,
        content=body,
        embedding=_distinct_embedding(index, surface=False),
        chunk_index=0,
        doc_type="adr",
        lifecycle_status="active",
        project="CAS",
    )


async def _seed(store, *, derived: bool) -> dict[str, str]:
    """Seed the crowded corpus on both surfaces and return its ids."""
    for index, (document_id, title, body) in enumerate(_CORPUS):
        await store.index_chunks(
            document_id, [_passage(index, document_id, title, body, derived=derived)]
        )
        surface = compose_document_surface(document_id, _document(document_id, title))
        surface.embedding = _distinct_embedding(index, surface=True)
        await store.upsert_document_surface(surface)
    return {document_id: title for document_id, title, _ in _CORPUS}


@pytest.fixture
async def corpus(store):
    """The corpus as a migrated vault holds it."""
    return await _seed(store, derived=True)


async def _rank_of(store, query: str, document_id: str) -> int | None:
    """Position of ``document_id`` in the results for ``query``, or None."""
    results = await store.search_bm25(query, limit=len(_CORPUS))
    for position, result in enumerate(results):
        if result.document_id == document_id:
            return position
    return None


# ---------------------------------------------------------------------------
# The corpus itself must be able to fail these tests
# ---------------------------------------------------------------------------


async def test_corpus_shares_a_working_vocabulary(store, corpus):
    """The corpus is crowded on the terms these titles are built from.

    The control on every other test in this module: ranking first has to be a
    result rather than an artifact of being the only document in the store.

    Crowding is measured on single terms rather than on whole titles, because
    matching is conjunctive over the document (CAS-ADR-048) -- a full-title
    query requires every one of its terms, so it is selective by construction
    and would report a thin corpus even from a dense one. What matters is that
    the *words* these titles are built from are contested, which is what makes
    a ranking decision necessary at all.
    """
    for term in ("retrieval", "engine", "surface", "text"):
        results = await store.search_bm25(term, limit=len(_CORPUS))
        assert len(results) >= 3, (
            f"{term!r} drew only {len(results)} documents; the corpus does not "
            "share enough vocabulary for a rank-1 assertion to mean anything"
        )


async def test_a_title_query_is_answered_at_all(store, corpus):
    """Every title returns at least its own document.

    Separates the two ways a rank assertion could be vacuous: ranking first
    out of nothing, and returning nothing at all. ``_rank_of`` yields ``None``
    in the second case and ``None == 0`` is false, so the rank tests already
    catch it -- stating it directly makes such a failure read as "not found"
    rather than "not first".
    """
    for document_id, title in corpus.items():
        results = await store.search_bm25(title, limit=len(_CORPUS))
        assert results, f"{title!r} returned nothing at all"
        assert document_id in {r.document_id for r in results}


# ---------------------------------------------------------------------------
# Rank 1 across the renderings a caller might type
# ---------------------------------------------------------------------------


async def test_exact_title_ranks_first(store, corpus):
    """Every document is the top result for its own title, verbatim."""
    for document_id, title in corpus.items():
        assert await _rank_of(store, title, document_id) == 0, (
            f"{document_id!r} did not rank first for its own title {title!r}"
        )


@pytest.mark.parametrize("transform", [str.lower, str.upper])
async def test_case_variant_title_ranks_first(store, corpus, transform):
    """Case is not something a caller should have to reproduce."""
    for document_id, title in corpus.items():
        query = transform(title)
        assert await _rank_of(store, query, document_id) == 0, (
            f"{document_id!r} did not rank first for {query!r}"
        )


async def test_underscore_variant_title_ranks_first(store, corpus):
    """Spaces typed as underscores still reach the document."""
    for document_id, title in corpus.items():
        query = title.replace(" ", "_")
        assert await _rank_of(store, query, document_id) == 0, (
            f"{document_id!r} did not rank first for {query!r}"
        )


async def test_hyphen_variant_title_ranks_first(store, corpus):
    """Hyphens and spaces are interchangeable in both directions.

    The direction that fails without a separator fold: the full-text parser
    reads ``ADR-001`` as the word ``adr`` followed by the signed integer
    ``-001``, so an index built from the raw title carries ``-001`` while a
    space-separated query asks for ``001``.
    """
    for document_id, title in corpus.items():
        for query in (title.replace("-", " "), title.replace(" ", "-")):
            assert await _rank_of(store, query, document_id) == 0, (
                f"{document_id!r} did not rank first for {query!r}"
            )


async def test_prefix_number_identifier_ranks_first(store, corpus):
    """The identifier shape every ADR and ticket title carries.

    Stated separately from the sweep above because it is the specific
    tokenization defect this work closes, and a sweep that stopped covering it
    would go quiet rather than red.
    """
    for query in ("ADR 001", "adr-001", "ADR_001", "adr 001"):
        assert await _rank_of(store, query, "00000001_adr_001") == 0, (
            f"the identifier query {query!r} did not reach its document first"
        )


async def test_camel_case_variant_ranks_first(store, corpus):
    """A compound identifier is reachable by its constituent words, and back.

    Both directions: a camelCase title found by spaced words, and a spaced
    title found by a camelCase query.
    """
    assert await _rank_of(store, "document level text normalization", "0000000a_camel") == 0
    assert await _rank_of(store, "documentLevelText normalization", "0000000a_camel") == 0
    assert await _rank_of(store, "documentLevelTextHandling", "0000000b_spaced") == 0


async def test_document_surface_competes_on_the_semantic_arm(store, corpus):
    """Document-level text is indexed for vector retrieval, not only keyword.

    CAS-ADR-049 puts the document surface on both arms. Before it existed, the
    synthetic header row was a first-class semantic candidate; losing that
    would strip every document of its title, tags and abstract on the vector
    side, which no keyword assertion in this module would notice.

    Each seeded row carries a vector unique to it, so retrieving one names
    exactly which row answered.
    """
    document_id = _CORPUS[3][0]
    index = 3

    surface_hits = await store.search_semantic(_distinct_embedding(index, surface=True), limit=3)
    assert surface_hits[0].document_id == document_id
    assert surface_hits[0].heading_path == "", (
        "the top hit should be the document-level row, which carries no heading"
    )
    # Both properties, because the empty heading is not what identifies the
    # row: a document with no headings has a genuine passage whose path is
    # also empty. The flag is what the row says about itself.
    assert surface_hits[0].is_document_surface is True, (
        "the top hit does not name itself a document-level row"
    )

    passage_hits = await store.search_semantic(_distinct_embedding(index, surface=False), limit=3)
    assert passage_hits[0].document_id == document_id
    assert passage_hits[0].heading_path == (f"{_CORPUS[index][1]}{HEADING_PATH_SEPARATOR}Body"), (
        "a passage vector must still retrieve the passage, not the surface -- "
        "and it is returned at its address, title root included, which the "
        "indexed structure does not change"
    )
    assert passage_hits[0].is_document_surface is False, (
        "a passage row is marked as a document-level one"
    )


# ---------------------------------------------------------------------------
# Recall the single-surface layout could not reach
# ---------------------------------------------------------------------------


async def test_title_reaches_a_document_whose_headings_never_carried_it(store):
    """A document whose heading paths never restated its title still matches.

    The case that could not pass while the title reached matching only through
    passage heading paths. A document from a source format that does not make
    its title a heading -- a word processor file, a spreadsheet -- had no route
    to its own title at all.
    """
    await store.index_chunks(
        "00000013_docx",
        [
            Chunk(
                document_id="00000013_docx",
                heading_path="Table of Contents",
                content="body prose sharing no term with the title",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=0,
            )
        ],
    )
    doc = _document("00000013_docx", "Thetaword Quarterly Review")
    surface = compose_document_surface("00000013_docx", doc)
    surface.embedding = [0.0] * EMBEDDING_DIM
    await store.upsert_document_surface(surface)

    hits = await store.search_bm25("thetaword", limit=10)
    assert [r.document_id for r in hits] == ["00000013_docx"]
    assert await _rank_of(store, "Thetaword Quarterly Review", "00000013_docx") == 0


# ---------------------------------------------------------------------------
# The gate must hold on the binding alone
# ---------------------------------------------------------------------------


async def test_gate_does_not_depend_on_a_graph_store_boost(store, corpus):
    """These results come from the retrieval binding, not a metadata boost.

    The service layer separately boosts documents whose title or tags contain
    the query as a substring, reading the graph store. That boost could mask a
    binding that had lost title matching entirely -- and it matches by
    substring, so it cannot answer the separator variants above at all. Every
    assertion in this module calls the content store directly, with no service
    layer and no graph store in the picture; this test states that invariant so
    it is not lost if the module is later refactored to run through the
    service.
    """
    assert not hasattr(store, "_graph"), (
        "the content store must have no graph-store collaborator; if it gains "
        "one, these results are no longer evidence about the binding alone"
    )
    # A term carried only by the title, queried in a form no substring search
    # could answer: 'adr 001' is not a substring of 'ADR-001: ...'.
    assert await _rank_of(store, "ADR 001 LangGraph", "00000001_adr_001") == 0


# ---------------------------------------------------------------------------
# Provenance still holds under all of the above
# ---------------------------------------------------------------------------


async def test_normalization_does_not_make_derived_text_matchable(store):
    """Widening the authored half must not widen the derived one.

    The risk the normalization introduces: if the expansion were applied to
    the whole document-level row rather than to its authored half, a filename
    stem or a generated abstract would become matchable through its normalized
    rendering even though the raw form is barred.
    """
    await store.index_chunks(
        "00000014_d1",
        [
            Chunk(
                document_id="00000014_d1",
                heading_path="Body",
                content="ordinary prose",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=0,
            )
        ],
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="00000014_d1",
            matchable="Ordinary Title",
            orienting="iotaword-quarterly-review IotaWordQuarterly",
            embedding=[0.0] * EMBEDDING_DIM,
        )
    )

    for query in ("iotaword", "iota word quarterly", "IotaWordQuarterly", "iotaword_quarterly"):
        assert await store.search_bm25(query, limit=10) == [], (
            f"derived text became matchable through its normalized form via {query!r}"
        )
    control = await store.search_bm25("ordinary title", limit=10)
    assert [r.document_id for r in control] == ["00000014_d1"], (
        "positive control: authored text on the same row still matches"
    )
