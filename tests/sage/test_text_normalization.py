"""Separator and compound-identifier normalization for document-surface matching.

CAS-ADR-049 places a document's authored title and tags on a retrieval surface
of their own. For a caller's query to reach that surface regardless of how the
title's separators and word boundaries were typed, the indexed text and the
query are normalized by two different transforms:

``expand_for_index`` widens indexed text to a superset of its renderings, so a
lexeme a caller might type is present however the author wrote it.
``fold_for_query`` narrows a query to the renderings every form shares, so a
conjunctive query does not demand a compound the index never carried.

The asymmetry is load-bearing and is pinned by
``test_index_superset_and_query_fold_are_deliberately_asymmetric``.
"""

import pytest

from sage.utils.text_normalization import expand_for_index, fold_for_query


def _lexemes(text: str) -> set[str]:
    """Whitespace tokens, lowercased, stripped of edge punctuation.

    A tokenizer-independent stand-in: edge punctuation is dropped because the
    full-text parser drops it too, so ``001:`` and ``001`` must not read as
    different tokens here when Postgres would treat them as one. The real gate
    runs these strings through Postgres; these unit tests assert the string
    transform only, so they stay fast and backend-free.
    """
    return {stripped for tok in text.split() if (stripped := tok.strip(":;,.()[]{}\"'").lower())}


# ---------------------------------------------------------------------------
# fold_for_query: separators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_tokens"),
    [
        ("ADR-001", {"adr", "001"}),
        ("doc_type_name", {"doc", "type", "name"}),
        ("CAS-ADR-049", {"cas", "adr", "049"}),
        ("mixed-sep_and space", {"mixed", "sep", "and", "space"}),
    ],
)
def test_fold_for_query_folds_separators(raw: str, expected_tokens: set[str]) -> None:
    """Hyphens and underscores become word boundaries.

    Regression guard for the tokenizer defect this work exists to close:
    Postgres reads ``ADR-001`` as the word ``adr`` followed by the signed
    integer ``-001``, so an index built from the raw string carries ``-001``
    while a space-separated query asks for ``001`` and never matches.
    """
    assert _lexemes(fold_for_query(raw)) == expected_tokens


def test_fold_for_query_drops_the_unsplit_compound() -> None:
    """A folded query must not demand a lexeme the index never carried.

    ``fold_for_query`` is used to build a conjunctive tsquery, so every token
    it emits becomes a requirement. Emitting ``documentleveltext`` alongside
    its parts would make the query unsatisfiable against a document whose
    title is the spaced form.
    """
    folded = _lexemes(fold_for_query("documentLevelText"))
    assert folded == {"document", "level", "text"}
    assert "documentleveltext" not in folded


# ---------------------------------------------------------------------------
# fold_for_query: compound identifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_tokens"),
    [
        ("documentLevelText", {"document", "level", "text"}),
        ("PortfolioDashboard", {"portfolio", "dashboard"}),
        ("LangGraph", {"lang", "graph"}),
    ],
)
def test_fold_for_query_splits_compound_identifiers(raw: str, expected_tokens: set[str]) -> None:
    """camelCase and PascalCase compounds split into their constituent words."""
    assert _lexemes(fold_for_query(raw)) == expected_tokens


@pytest.mark.parametrize("raw", ["langgraph", "XLSX", "PV07", "v3", "sage"])
def test_fold_for_query_leaves_unsplittable_tokens_whole(raw: str) -> None:
    """A token with no case or separator boundary survives intact.

    Without this, an all-lowercase query like ``langgraph`` would be mangled
    into something the index cannot satisfy. It is also the half of the
    asymmetry that makes ``expand_for_index``'s superset necessary.
    """
    assert _lexemes(fold_for_query(raw)) == {raw.lower()}


# ---------------------------------------------------------------------------
# expand_for_index: superset property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "must_contain"),
    [
        ("LangGraph", {"langgraph", "lang", "graph"}),
        ("ADR-001", {"adr", "001"}),
        ("PortfolioDashboard", {"portfoliodashboard", "portfolio", "dashboard"}),
    ],
)
def test_expand_for_index_keeps_both_the_compound_and_its_parts(
    raw: str, must_contain: set[str]
) -> None:
    """Indexed text carries every rendering a caller might type.

    The unsplit form must survive so a lowercase compound query
    (``langgraph``), which cannot be split without a dictionary, still finds
    a document whose author wrote ``LangGraph``.
    """
    assert must_contain <= _lexemes(expand_for_index(raw))


def test_expand_for_index_is_a_superset_of_fold_for_query() -> None:
    """Whatever a folded query asks for, expanded index text supplies.

    This is the invariant that makes the pair correct: for the *same* source
    string the query can never out-demand the index.
    """
    for raw in [
        "ADR-001: LangGraph as ROOT Harness orchestration engine",
        "documentLevelText",
        "Document-level text is a separate retrieval surface",
        "doc_type_name",
    ]:
        assert _lexemes(fold_for_query(raw)) <= _lexemes(expand_for_index(raw)), raw


# ---------------------------------------------------------------------------
# The asymmetry itself
# ---------------------------------------------------------------------------


def test_index_superset_and_query_fold_are_deliberately_asymmetric() -> None:
    """The two transforms must not collapse into one.

    A single symmetric transform cannot satisfy both directions at once:
    keeping the compound breaks a camelCase query against a spaced title,
    and dropping it breaks a lowercase-compound query against a camelCase
    title. If someone later aliases one function to the other, this fails.
    """
    assert _lexemes(expand_for_index("LangGraph")) != _lexemes(fold_for_query("LangGraph"))


@pytest.mark.parametrize(
    ("title", "query"),
    [
        # spaced title reached by a camelCase query
        ("Document Level Text", "documentLevelText"),
        # camelCase title reached by a lowercase compound query
        ("LangGraph orchestration", "langgraph orchestration"),
        # hyphenated identifier reached by a spaced query, and the reverse
        ("ADR-001: orchestration engine", "ADR 001 orchestration engine"),
        ("ADR 001 orchestration engine", "ADR-001: orchestration engine"),
        # underscore and case variants
        ("Document Level Text", "document_level_text"),
        ("Document Level Text", "DOCUMENT-LEVEL-TEXT"),
    ],
)
def test_folded_query_is_satisfied_by_expanded_title(title: str, query: str) -> None:
    """The end-to-end property the title rank-1 gate depends on.

    Every token a folded query requires is present in the expanded title, so
    a conjunctive match succeeds for each way the caller might have typed it.
    """
    assert _lexemes(fold_for_query(query)) <= _lexemes(expand_for_index(title)), (
        f"query {query!r} demands "
        f"{_lexemes(fold_for_query(query)) - _lexemes(expand_for_index(title))} "
        f"which title {title!r} does not carry"
    )


# ---------------------------------------------------------------------------
# Idempotence and non-destruction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["ADR-001: LangGraph", "documentLevelText", "plain words here", "doc_type_name"],
)
def test_both_transforms_are_idempotent(raw: str) -> None:
    """Re-normalizing changes nothing.

    The migration may re-run over rows it already rewrote, and a query may be
    folded by more than one layer; neither may drift.
    """
    assert _lexemes(fold_for_query(fold_for_query(raw))) == _lexemes(fold_for_query(raw))
    assert _lexemes(expand_for_index(expand_for_index(raw))) == _lexemes(expand_for_index(raw))


def test_transforms_preserve_authored_words() -> None:
    """Normalization adds renderings; it never drops an authored word."""
    title = "Document-level text is a separate retrieval surface"
    authored = {"text", "is", "a", "separate", "retrieval", "surface"}
    assert authored <= _lexemes(expand_for_index(title))
    assert authored <= _lexemes(fold_for_query(title))


@pytest.mark.parametrize("transform", [expand_for_index, fold_for_query])
def test_empty_input_yields_empty_output(transform) -> None:
    """Documents without tags, and blank queries, must not raise."""
    assert transform("") == ""
    assert transform("   ").strip() == ""


# ---------------------------------------------------------------------------
# Compound shapes carried over from the identifier expansion this replaced
# ---------------------------------------------------------------------------


def test_fold_for_query_splits_an_acronym_run_from_the_word_after_it() -> None:
    """A leading acronym separates from the title-cased word that follows.

    ``NPScopeManifest`` is three words, not two: the pattern's acronym-run
    alternative has to give back its final capital to the word starting there.
    A split that took the whole run would yield ``npscope``, which no caller
    would type.
    """
    assert _lexemes(fold_for_query("NPScopeManifest")) == {"np", "scope", "manifest"}


def test_expand_for_index_deduplicates_repeated_renderings() -> None:
    """A token contributed twice appears once.

    Indexed text is the union of a string's renderings, and a word that folds
    to itself is contributed by both. Repeating it inflates its term frequency
    and would let an author raise a document's own ranking by writing a title
    whose words survive the fold unchanged.
    """
    expanded = expand_for_index("Template_Template").split()
    assert expanded.count("Template") == 1, expanded


def test_expand_for_index_handles_a_mixed_compound_and_version_token() -> None:
    """The whole shape a source filename tends to carry, in one string."""
    tokens = _lexemes(expand_for_index("PortfolioDashboard_Template_v3"))
    assert {"portfolio", "dashboard", "template", "v3"} <= tokens
