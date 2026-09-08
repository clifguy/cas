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

Widening the split widens both transforms at once, since the expansion is
defined over the fold, and that raises the question of whether stored index
text has to be rebuilt to stay in step. It does not. The folded rendering
reaches the keyword binding as an arm *added* to the arms the query's own
rendering produces, never as a substitution for them, so a row indexed under a
narrower split stays reachable by the spelling it stored -- the new arm can
only admit documents, not withdraw them. What such a row does not yet have is
the widened expansion, so the reverse direction (a separated query reaching the
compound it was written as) waits for the row to be composed again, which
ingest and any edit to an authored field both do. A widening not yet applied,
rather than a match lost.
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

    A candidate is an alphabetic token carrying an *internal* capital, so
    ``PortfolioDashboard``, ``documentLevelText`` and ``graphLevel`` come apart
    while ``Document`` does not. Position rather than count is what separates
    those: a single Title-cased word carries a capital too, and only where it
    sits distinguishes it from a two-word ``lowerCamel`` compound, which counts
    the same one. Counting them refuses that compound as collateral, and a
    document titled *Graph Level* is then unreachable by ``graphLevel``.

    Four guards, and it is worth naming which does what, because they are not
    interchangeable and only one of them is about safety:

    - ``isalpha`` bounds what kind of token is a candidate at all. A mixed
      letter-and-digit token is left alone as a matter of policy: ``PV07`` and
      ``v3`` are identifiers a caller types whole.
    - The internal-capital test says what a compound *is*, and is the rule this
      function turns on.
    - ``len(parts) >= 2`` keeps a single word whole. The pattern consumes
      ``Document``, ``XLSX`` and ``ADR`` each in one match, so each is returned
      unchanged rather than as its own rewrite.
    - The parts must reassemble into the token, and this is the safety one. The
      pattern's alternatives are ASCII while ``isalpha`` is not, so a word
      carrying a letter outside that range is matched in pieces *around* it and
      the pieces do not add back up -- ``caféLevel`` yields ``caf`` and
      ``Level``, the ``é`` in neither. Splitting there would drop the letter
      that distinguished the word and index a lexeme no caller could type.

    The last guard is stated over the token rather than over an alphabet
    deliberately. The defect is a disagreement between the pattern's reach and
    the gate's, so a rule keyed on the disagreement itself stays true if either
    side later moves; one keyed on today's alphabet would not.
    """
    if not (token.isalpha() and any(ch.isupper() for ch in token[1:])):
        return [token]
    parts = _COMPOUND_PARTS.findall(token)
    return parts if len(parts) >= 2 and "".join(parts) == token else [token]


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
