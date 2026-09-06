"""A passage's structure relative to its document (CAS-ADR-049 Decision 3).

One function, used by both writers: the indexing pipeline derives this text as a
document is ingested, and the migration derives it for a vault provisioned
before the column existed. Keeping the derivation in one place is what stops a
migrated vault and a freshly ingested one from carrying different structure for
the same source.

The decision separates two roles a heading path was serving. The *address* is
the path as the source produced it, and it does not change: enumeration returns
it, a section read accepts it, and a stored or cached path goes on resolving.
The *indexed structure* is that path relative to the document, and it is what
the keyword arm ranks at its top weight. Where a source format makes the
document's title its top-level heading, that heading stays the passage's address
and leaves its indexed structure, because the title is document-level and the
document surface carries it.
"""

from sage.adapters.interfaces import HEADING_PATH_SEPARATOR


def indexed_structure(heading_path: str, document_title: str | None) -> str:
    """The heading path relative to its document.

    Removes the path's root element when, and only when, that element equals the
    document title. Everything else is returned whole, including a title that
    recurs deeper in the path -- a heading within the document, which
    CAS-ADR-049 keeps at its ranking weight.

    Equality is exact, after stripping surrounding whitespace from both sides.
    The two errors available here are not symmetric: a root left unstripped
    costs that document nothing but the pre-decision behaviour, while a root
    stripped that was not the title permanently removes real structural text
    from the top ranking weight. So the comparison widens only where a
    difference cannot carry meaning. Whitespace qualifies -- the markdown
    adapter already strips heading text, so a leading or trailing space is a
    pipeline artifact. Case does not: two headings differing in case are two
    different headings, and the address is what the source produced.

    An empty result is a legitimate value, not an absence. It is what the
    top-level heading's own passage carries, and the title still reaches that
    passage's index through its content, where the chunker prepends the heading
    line -- the decision demotes the title from the top weight rather than
    removing it.

    Args:
        heading_path: The passage's address, as the source adapter produced it.
        document_title: The stored document's title. When absent, nothing can be
            recognised as the title and the path is returned whole.

    Returns:
        The path relative to its document. Never ``None``: a caller storing this
        writes a derived value, and ``NULL`` in the column means underived.
    """
    if not heading_path:
        return ""

    title = (document_title or "").strip()
    if not title:
        return heading_path

    root, _separator, remainder = heading_path.partition(HEADING_PATH_SEPARATOR)
    if root.strip() != title:
        return heading_path
    return remainder
