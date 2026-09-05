"""StubContentStore: the keyword contract, pinned against the in-memory double.

The double stands in for the content-store port in most of the suite, so its
keyword semantics are not a convenience of the fixture -- they are the contract
every test written against it is evidence about. CAS-ADR-048 places match
semantics at the port rather than at any binding, which makes a double whose
matching disagrees with the contract a defect in the double.

These mirror the contract tests in ``test_content_store_postgres.py``
name-for-name, so the two read as one contract with two bindings and a rule
present on one side but absent on the other is visible as a naming gap. Unlike
those, nothing here needs a server: the double is the point, and a pin that
skipped when Postgres was unconfigured would be absent from exactly the runs
meant to catch drift.

The mirror is not total, and the inventory below is the whole of what it omits
-- read as a complete list, because that is how a name-for-name claim will be
read. The claim is bidirectional, so the inventory is too.

**Postgres rules with no counterpart here.** Quoted-phrase adjacency and the
within-chunk fallback path, because the double parses no operators, so neither
shape exists. Title matchability, a property of the ingestion projection rather
than of a store. The IN-list filter form: ``_chunk_matches_filters`` handles it
and nothing here exercises it, which is a gap rather than a property of the
double. And ``finds_content_term``, a single-term smoke assertion every other
test in this module subsumes.

**Rules pinned here in a weaker form.** A heading-only term is findable against
both, but only the binding ranks a heading match above a body one, so that
ordering belongs against a real backend. And the parse is pinned for its shape
rather than its content: the double's is a whitespace split, so the stopword,
stemming, negation and alternation cases the Postgres parse tests cover have
nothing to assert against here.

**Rules pinned here and nowhere else.** The four parse pins -- the two
directions of the agreement, the alternation flag, and the parse's own shape --
have no meaning against a binding whose parse *is* the text-search
configuration. ``header_sentinel_is_not_searchable_text`` is a genuine
divergence rather than an absence: the binding indexes the marker at the
heading weight today, so the rule holds for this double alone, and
``StubContentStore.search_bm25`` carries the reasoning. And the pin that the
header is not counted as a matched chunk is a contract rule that could carry a
Postgres twin and does not yet.

The scalar filter form is a naming gap rather than either: both filter tests
here exercise it, under names that do not match the Postgres ones.
"""

from __future__ import annotations

import pytest

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    Chunk,
    KeywordQueryParse,
)
from sage.adapters.stubs import StubContentStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(
    document_id: str,
    *,
    content: str,
    heading_path: str = "Section",
    chunk_index: int = 0,
    doc_type: str | None = None,
    lifecycle_status: str | None = None,
    project: str | None = None,
) -> Chunk:
    """A chunk with no embedding -- the keyword arm never reads one."""
    return Chunk(
        document_id=document_id,
        heading_path=heading_path,
        content=content,
        chunk_index=chunk_index,
        doc_type=doc_type,
        lifecycle_status=lifecycle_status,
        project=project,
    )


def _header(document_id: str, *, content: str) -> Chunk:
    """The synthetic document-header chunk, as the ingestion pipeline writes it."""
    return _chunk(
        document_id,
        content=content,
        heading_path=SYNTHETIC_HEADER_HEADING_PATH,
        chunk_index=-1,
    )


@pytest.fixture
def store() -> StubContentStore:
    return StubContentStore()


# ---------------------------------------------------------------------------
# Scope and strictness (CAS-ADR-048)
# ---------------------------------------------------------------------------


async def test_stub_search_bm25_empty_query_returns_empty(store):
    """A blank query searches for nothing, matching what the parse reports of it."""
    await store.index_chunks("d1", [_chunk("d1", content="something")])

    assert await store.search_bm25("", limit=10) == []
    assert await store.search_bm25("   ", limit=10) == []


async def test_stub_search_bm25_finds_heading_only_term(store):
    """A chunk's heading path is indexed text, so a term living only there matches.

    The rival searches ``content`` alone, which is what the double did before
    it was brought to the contract. A heading term is often the only place a
    section's subject is named, so a double blind to it makes a whole class of
    query untestable against the port.
    """
    await store.index_chunks(
        "d1",
        [_chunk("d1", content="unremarkable prose", heading_path="Zzheadingword")],
    )

    assert [r.document_id for r in await store.search_bm25("zzheadingword", limit=10)] == ["d1"], (
        "a term carried only by the heading path must match"
    )


async def test_stub_search_bm25_header_sentinel_is_not_searchable_text(store):
    """The header row's heading path is a marker, not a heading someone wrote.

    A rival that indexes every chunk's heading path uniformly makes the
    sentinel's own words searchable, and ``document`` is one of them. The
    Postgres binding is that rival today, so this pin is stub-only by
    construction rather than by omission; the reasoning for keeping it that way
    is in ``StubContentStore.search_bm25``. Only the score can see it: the
    sentinel cannot change what matches, because the
    header is already barred from the match union, and it cannot take the
    excerpt, because an authored chunk always outranks it in that tiebreak. So
    the terms are split one per authored chunk, leaving no chunk that carries
    the whole query -- unless the sentinel supplies the second term to the
    header row, which would score the document as though a single passage had.
    """
    await store.index_chunks(
        "d1",
        [
            _header("d1", content="Abstract: alphaword restated in the summary"),
            _chunk("d1", content="document scope of the surrounding text", heading_path="S1"),
            _chunk("d1", content="alphaword only here", heading_path="S2", chunk_index=1),
        ],
    )

    res = await store.search_bm25("document alphaword", limit=10)
    assert [r.document_id for r in res] == ["d1"], (
        "precondition: the authored chunks carry both terms between them"
    )
    assert res[0].score < 1.0, (
        "no authored chunk carries the whole query, so a full score means the "
        "sentinel supplied the term the header's own text does not"
    )


async def test_stub_search_bm25_requires_every_term_across_the_document(store):
    """Multi-term keyword queries are conjunctive: one absent term matches nothing.

    Single-term queries behave identically under AND and OR and so cannot tell
    the two apart; this is the case that can. The rival is the double's former
    ``matches > 0``, which returned anything carrying a subset.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword betaword gammaword")])

    present = await store.search_bm25("alphaword betaword gammaword", limit=10)
    assert [r.document_id for r in present] == ["d1"], "every term present must match"

    absent = await store.search_bm25("alphaword betaword gammaword deltaword", limit=10)
    assert absent == [], (
        "one absent term must cull the whole match under AND semantics; a "
        "subset-matching double still returns d1 on the three terms it carries"
    )


async def test_stub_search_bm25_terms_may_span_chunks_of_one_document(store):
    """The conjunction is per document, not per chunk (CAS-ADR-048).

    The union of the document's chunks carries both terms, so it matches even
    though no single chunk does. Scoping the conjunction to a chunk would make
    retrieval a function of where the projection happened to place a boundary.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
        ],
    )

    assert [r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)] == [
        "d1"
    ], "terms split across two chunks of one document must match the document"


async def test_stub_search_bm25_does_not_match_across_documents(store):
    """The union is per document, not corpus-wide.

    The sharper control on the scope move: a union taken over the whole store
    -- the natural over-correction when lifting the scope off the chunk --
    returns both documents here, where the split-across-chunks case alone
    would not distinguish it.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword only here")])
    await store.index_chunks("d2", [_chunk("d2", content="betaword only here")])

    assert await store.search_bm25("alphaword betaword", limit=10) == [], (
        "one term in each of two documents must not match either of them"
    )


async def test_stub_search_bm25_returns_one_row_per_matching_document(store):
    """A matching document is represented once, by its best-matching chunk.

    Three chunks all carry the term, so a row-per-chunk double returns three
    rows and this fails.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword first", chunk_index=0),
            _chunk("d1", content="alphaword second", chunk_index=1),
            _chunk("d1", content="alphaword third", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.document_id for r in res] == ["d1"], (
        "one row per matching document; a row-per-chunk double returns three"
    )


async def test_stub_search_bm25_limit_is_a_document_budget(store):
    """``limit`` bounds documents, not rows.

    Each document carries three matching chunks, so a row budget spends the
    whole of ``limit=2`` inside the first document and returns one distinct
    id. Asserting on the count of *distinct ids* rather than on the row count
    is what separates the two budgets -- both return two rows.
    """
    for doc_id in ("d1", "d2", "d3"):
        await store.index_chunks(
            doc_id,
            [
                _chunk(doc_id, content="alphaword first", chunk_index=0),
                _chunk(doc_id, content="alphaword second", chunk_index=1),
                _chunk(doc_id, content="alphaword third", chunk_index=2),
            ],
        )

    res = await store.search_bm25("alphaword", limit=2)
    assert len({r.document_id for r in res}) == 2, (
        "two documents, not two chunks of one; a row budget answers with one id"
    )


# ---------------------------------------------------------------------------
# Ranking, excerpt, and the matched-chunk count
# ---------------------------------------------------------------------------


async def test_stub_search_bm25_excerpt_is_the_best_matching_chunk(store):
    """Co-occurrence within a chunk is a ranking signal, not a matching one.

    The co-occurring chunk sits in the *middle*, so neither end of document
    order is the answer: a double that returns the first chunk fails, and so
    does one that returns the last. Two chunks cannot separate those rivals,
    because with two the best chunk is always at one end or the other.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword alone", heading_path="S1", chunk_index=0),
            _chunk("d1", content="alphaword betaword together", heading_path="S2", chunk_index=1),
            _chunk("d1", content="alphaword again alone", heading_path="S3", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in res] == ["d1"]
    assert res[0].heading_path == "S2", (
        "the excerpt is the chunk carrying both terms, not the first chunk in document order"
    )


async def test_stub_search_bm25_ranks_a_co_occurring_document_above_a_split_one(store):
    """Both documents match; the one whose chunk carries both terms ranks first.

    The co-occurring document is ``d2`` deliberately. Document id is the
    tiebreak within an equal score, so seeding it as ``d1`` would put the
    expected winner first under alphabetical order as well, and a double that
    ignored score entirely would pass -- which is the whole of what this test
    is for.
    """
    await store.index_chunks("d2", [_chunk("d2", content="alphaword betaword together")])
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert {r.document_id for r in res} == {"d1", "d2"}, "both documents match under document scope"
    assert res[0].document_id == "d2", (
        "co-occurrence within one chunk outranks the same terms split apart, "
        "against the alphabetical order of the ids"
    )


async def test_stub_search_bm25_reports_the_count_of_chunks_carrying_query_terms(store):
    """``matched_chunk_count`` counts the document's chunks carrying a query term.

    Two of three chunks carry the term, so the expected value distinguishes the
    dataclass default of 1 from a count of every chunk in the document.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword first", chunk_index=0),
            _chunk("d1", content="alphaword second", chunk_index=1),
            _chunk("d1", content="nothing relevant", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.matched_chunk_count for r in res] == [2], (
        "two of three chunks carry the term; neither 1 nor 3 is the answer"
    )


async def test_stub_search_bm25_counts_chunks_carrying_any_term_not_the_whole_query(store):
    """The count is of chunks carrying *a* query term, not chunks that match.

    A single-term query cannot tell those apart. Under the document-scoped
    match they part company: here no chunk carries both terms, so a count of
    chunks satisfying the whole query is zero while the count of chunks
    carrying one is two.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
            _chunk("d1", content="nothing relevant", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.matched_chunk_count for r in res] == [2], (
        "both term-bearing chunks count, though neither satisfies the query alone"
    )


# ---------------------------------------------------------------------------
# The search and the parse agree
# ---------------------------------------------------------------------------


async def test_stub_search_bm25_requires_the_terms_its_own_parse_reports(store, monkeypatch):
    """The search requires what the parse reports, not a split of the query text.

    Anti-coincidental trap for reading ``query.lower().split()``. The double's
    own parse *is* a word split, so that rival is indistinguishable from the
    real one in every other test in this file; only a parse whose output cannot
    be confused with a split separates them. What is at stake is the double
    stating one matching rule and applying another -- the divergence this pin
    exists to keep closed, from either side.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword betaword")])

    control = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in control] == ["d1"], (
        "precondition: the indexed corpus must be searchable"
    )

    async def _parse(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("zzlexeme",), excluded=(), all_required=True, adjacent=False
        )

    monkeypatch.setattr(store, "parse_keyword_query", _parse)

    assert await store.search_bm25("alphaword betaword", limit=10) == [], (
        "the required term is absent from the corpus; matching the typed words "
        "instead means the search split the query text itself"
    )


async def test_stub_search_bm25_matches_a_parsed_term_the_query_text_lacks(store, monkeypatch):
    """The parse is the whole of what the search requires, not an addition to it.

    The positive half of the pin above, and the two are not redundant: that one
    reds a search reading the query text *instead of* the parse, and this one
    reds a search reading both. A rival taking the union of the two --
    ``(*parse.terms, *query.lower().split())``, the belt-and-braces shape a
    later edit reaches for -- satisfies the negative direction, because the
    absent term culls the match there either way. Here the union rival requires
    ``zzz`` on top of the reported term and matches nothing, while a search
    reading only the query text has no reported term to look for at all. One
    fixture, both rivals.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword only here")])

    async def _parse(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("alphaword",), excluded=(), all_required=True, adjacent=False
        )

    monkeypatch.setattr(store, "parse_keyword_query", _parse)

    res = await store.search_bm25("zzz", limit=10)
    assert [r.document_id for r in res] == ["d1"], (
        "the reported term is present and is the only requirement; a search "
        "that also required the typed word would match nothing"
    )
    assert res[0].score == 1.0, "the ranking pool reads the reported term too, not the typed one"


async def test_stub_search_bm25_honours_a_parse_that_does_not_require_every_term(
    store, monkeypatch
):
    """A parse reporting alternatives is not conjunctive, and must not be applied as one.

    Anti-coincidental trap for hard-coding ``all(...)``: the reported terms are
    identical either way, so only the flag separates a conjunction from an
    alternation.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword only here")])

    async def _parse_alternation(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("alphaword", "betaword"), excluded=(), all_required=False, adjacent=False
        )

    monkeypatch.setattr(store, "parse_keyword_query", _parse_alternation)

    assert [r.document_id for r in await store.search_bm25("alphaword or betaword", limit=10)] == [
        "d1"
    ], "a document satisfying one side of an alternation matches"


# ---------------------------------------------------------------------------
# Filters apply at the matching unit
# ---------------------------------------------------------------------------


async def test_stub_filter_applies_to_the_match_union_not_just_the_ranking_pool(store):
    """Predicates select the slice, then the union is taken inside it.

    A filter may only narrow: it must admit no document the equivalent
    unfiltered search excludes, and it must not admit one whose terms are
    spread across chunks it does not select. The rival takes the union over
    every chunk and filters the resulting rows, which returns d1 here. The
    unfiltered call is the positive control, so the empty filtered result is
    about the slice rather than about the seed.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", lifecycle_status="active", chunk_index=0),
            _chunk("d1", content="betaword only here", lifecycle_status="archived", chunk_index=1),
        ],
    )

    unfiltered = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in unfiltered] == ["d1"], (
        "precondition: the union across both chunks matches"
    )

    filtered = await store.search_bm25(
        "alphaword betaword", limit=10, filters={"lifecycle_status": "active"}
    )
    assert filtered == [], "the archived chunk is outside the slice, so it cannot supply betaword"


async def test_stub_filtered_keyword_search_admits_no_document_the_unfiltered_one_excludes(store):
    """The narrowing direction: a filter subtracts documents, it never adds one.

    The test above pins that a filter must not admit a document whose terms are
    spread across chunks it did not select. This is the other half of "a filter
    only narrows", and it is a different rival: one whose predicates reach the
    match union but not the ranking pool, or that applies the slice to one and
    the whole document to the other, can return a document under a filter that
    the unfiltered search never returned. Both filter values are exercised, so
    a predicate ignored outright is caught too -- it would return both
    documents each time.
    """
    await store.index_chunks(
        "d1", [_chunk("d1", content="alphaword here", lifecycle_status="active")]
    )
    await store.index_chunks(
        "d2", [_chunk("d2", content="alphaword here", lifecycle_status="archived")]
    )

    unfiltered = {r.document_id for r in await store.search_bm25("alphaword", limit=10)}
    assert unfiltered == {"d1", "d2"}, "precondition: both documents match unfiltered"

    for value, expected in (("active", {"d1"}), ("archived", {"d2"})):
        filtered = {
            r.document_id
            for r in await store.search_bm25(
                "alphaword", limit=10, filters={"lifecycle_status": value}
            )
        }
        assert filtered <= unfiltered, "a filter may not admit what the unfiltered search excluded"
        assert filtered == expected, f"the {value} slice holds exactly one of the two"


async def test_stub_parse_keyword_query_reports_a_whitespace_split_and_models_nothing_else(store):
    """The double's parse shape is load-bearing, so it is pinned rather than assumed.

    The retrieval service reads all four fields to build its keyword
    advisories, and every advisory test in the suite runs against this parse.
    What the double reports is therefore a property callers depend on, not an
    implementation detail: the terms are the query lowercased and split on
    whitespace -- no stopword removal, no stemming -- with no exclusions, every
    term required, and no adjacency.
    """
    parse = await store.parse_keyword_query("The Running AlphaWord")

    assert parse.terms == ("the", "running", "alphaword"), (
        "a whitespace split, lowercased; a stopword filter would drop 'the' "
        "and a stemmer would report 'run'"
    )
    assert parse.excluded == ()
    assert parse.all_required is True
    assert parse.adjacent is False

    blank = await store.parse_keyword_query("   ")
    assert blank.terms == (), (
        "a blank query carries no terms, matching what the search does with it"
    )


# ---------------------------------------------------------------------------
# Provenance: derived text ranks but never matches (CAS-ADR-049)
# ---------------------------------------------------------------------------


async def test_stub_search_bm25_header_only_term_does_not_match(store):
    """A term present only in derived text cannot make a document match."""
    await store.index_chunks(
        "d1",
        [
            _header("d1", content="Abstract: zzabstractterm governs every boundary"),
            _chunk("d1", content="ordinary body prose", chunk_index=0),
        ],
    )

    assert await store.search_bm25("zzabstractterm", limit=10) == [], (
        "a generated abstract is evidence about a document, not content of it"
    )


async def test_stub_search_bm25_header_term_cannot_complete_a_conjunction(store):
    """Derived text cannot supply the term the authored text is missing.

    The sharper form of the rule: document scope lets terms combine freely
    across a document, so without this the header would silently become a
    universal donor for any conjunction it happens to complete.
    """
    await store.index_chunks(
        "d1",
        [
            _header("d1", content="Abstract: betaword appears only in the generated summary"),
            _chunk("d1", content="alphaword appears in the body", chunk_index=0),
        ],
    )

    assert await store.search_bm25("alphaword betaword", limit=10) == [], (
        "the header may not complete a conjunction the authored text leaves open"
    )
    assert [r.document_id for r in await store.search_bm25("alphaword", limit=10)] == ["d1"], (
        "the authored term alone still matches -- the header is excluded from "
        "matching, not the document from the store"
    )


async def test_stub_search_bm25_header_is_never_the_excerpt(store):
    """The header may set a document's score; it may not be what the caller reads.

    Otherwise a query for a word in the header's own scaffolding answers with
    that scaffolding. The header carries both terms and the body only one, so
    a double picking the highest-scoring chunk outright returns the header.
    """
    await store.index_chunks(
        "d1",
        [
            _header("d1", content="Abstract: alphaword and betaword both restated here"),
            _chunk("d1", content="alphaword betaword in the body", heading_path="S1"),
            _chunk("d1", content="alphaword alone", heading_path="S2", chunk_index=1),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in res] == ["d1"]
    assert res[0].heading_path == "S1", "the excerpt is drawn from an authored passage"


async def test_stub_search_bm25_header_is_not_counted_as_a_matched_chunk(store):
    """``matched_chunk_count`` counts authored passages bearing on the query.

    The header carries the term too, so a count over every term-bearing chunk
    answers 2 where the authored count is 1.
    """
    await store.index_chunks(
        "d1",
        [
            _header("d1", content="Abstract: alphaword restated"),
            _chunk("d1", content="alphaword in the body", chunk_index=0),
        ],
    )

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.matched_chunk_count for r in res] == [1], (
        "one authored chunk carries the term; counting the header answers two"
    )


async def test_stub_search_bm25_header_still_ranks_a_matched_document(store):
    """Derived text keeps its ranking value on a document that does match.

    The control against excluding the header outright: it is barred from
    satisfying a match, not removed from the store or from ranking. The two
    documents carry identical authored text -- the terms split one per chunk,
    so no authored chunk carries both -- and the only thing separating their
    scores is that d1's header restates both.
    """
    for doc_id, header_text in (
        ("d1", "Abstract: alphaword and betaword both restated"),
        ("d2", "Abstract: nothing of relevance to the query"),
    ):
        await store.index_chunks(
            doc_id,
            [
                _header(doc_id, content=header_text),
                _chunk(doc_id, content="alphaword only here", heading_path="S1", chunk_index=0),
                _chunk(doc_id, content="betaword only here", heading_path="S2", chunk_index=1),
            ],
        )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in res] == ["d1", "d2"], (
        "both match on authored text; the richer header outranks the barer one, "
        "so the header is still in the ranking pool"
    )
    assert res[0].score > res[1].score
    assert res[0].heading_path != SYNTHETIC_HEADER_HEADING_PATH, (
        "the header set the score without becoming the excerpt"
    )
