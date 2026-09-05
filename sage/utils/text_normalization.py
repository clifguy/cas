"""Separator and compound-identifier normalization for document-surface matching.

CAS-ADR-049 carries a document's authored title and tags on a retrieval surface
of their own, where they may satisfy a caller's match. Reaching them reliably
means a caller should not have to reproduce an author's separators or word
boundaries: ``ADR-001``, ``ADR 001`` and ``adr_001`` name the same thing, as do
``documentLevelText`` and ``document level text``.

The full-text configuration does not supply that on its own. It reads
``ADR-001`` as the word ``adr`` followed by the signed integer ``-001``, so an
index built from the raw string carries ``-001`` while a space-separated query
asks for ``001``; and it collapses ``documentLevelText`` to one opaque lexeme
rather than three words.

Two transforms close the gap, and they are deliberately not the same one:

``expand_for_index``
    Widens indexed text to a *superset* of its renderings -- the original text
    plus its folded and split forms. An all-lowercase compound query such as
    ``langgraph`` cannot be split without a dictionary, so the unsplit lexeme
    has to survive on the index side for that query to land.

``fold_for_query``
    Narrows a query to the renderings every form shares, *replacing* a compound
    with its parts rather than adding them. A query becomes a conjunction, so
    every token it emits is a requirement; emitting ``documentleveltext``
    alongside its parts would make the query unsatisfiable against a document
    whose title is the spaced form.

The resulting invariant is that for any one source string, what a folded query
requires is always a subset of what expanded index text supplies.
"""

import re

__all__ = ["expand_for_index", "fold_for_query"]


# Hyphen and underscore are word boundaries for our purposes; the full-text
# parser treats neither the way a caller means it.
_SEPARATORS = re.compile(r"[-_]+")

# Constituent runs of a compound identifier: a title-cased word, an acronym
# run, a lowercase run, or a digit run. Mirrors the split already used when
# composing identifier tokens at ingest.
_COMPOUND_PARTS = re.compile(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|$)|[a-z]+|[0-9]+")


def _split_compound(token: str) -> list[str]:
    """Return a token's constituent words, or ``[token]`` when it has none.

    Only alphabetic tokens carrying two or more capitals are candidates, so
    ``XLSX`` and ``langgraph`` pass through whole while ``PortfolioDashboard``
    and ``documentLevelText`` come apart. A token that yields a single part is
    returned unchanged rather than as its own rewrite.
    """
    if not (token.isalpha() and sum(1 for ch in token if ch.isupper()) >= 2):
        return [token]
    parts = _COMPOUND_PARTS.findall(token)
    return parts if len(parts) >= 2 else [token]


def fold_for_query(text: str) -> str:
    """Return ``text`` with separators folded and compounds replaced by parts.

    Used to build the query side of a document-surface match. Compounds are
    *replaced* rather than augmented, so the conjunction a caller's query
    becomes cannot demand a lexeme the index never carried.

    Args:
        text: Raw caller query text, or authored text being compared to one.

    Returns:
        Space-separated text carrying the shared renderings. Empty for input
        that contributes no tokens.
    """
    if not text:
        return ""
    folded = _SEPARATORS.sub(" ", text)
    out: list[str] = []
    for token in folded.split():
        out.extend(_split_compound(token))
    return " ".join(out)


def expand_for_index(text: str) -> str:
    """Return ``text`` widened to a superset of its renderings.

    Used for the authored text written to the document surface. The original
    is kept alongside its folded and split forms so that a query naming either
    the compound or its parts finds the document.

    Args:
        text: Authored text -- a document title, or its tags joined.

    Returns:
        Space-separated text carrying every rendering, first occurrence
        preserved and duplicates dropped. Empty for input that contributes no
        tokens.
    """
    if not text:
        return ""
    seen: dict[str, None] = {}
    for token in [*text.split(), *fold_for_query(text).split()]:
        seen.setdefault(token, None)
    return " ".join(seen)
