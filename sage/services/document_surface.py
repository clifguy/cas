"""Composition of a document's document-level retrieval row (CAS-ADR-049).

One function, used by both writers: the indexing pipeline composes this row as
a document is ingested and again once its abstract is generated, and the
migration composes it for a vault provisioned before the surface existed.
Keeping the composition in one place is what stops a migrated vault and a
freshly ingested one from carrying different text under the same contract.
"""

from sage.adapters.interfaces import DocumentSurface
from sage.models.schemas import Document
from sage.utils.text_normalization import expand_for_index


def source_path_stem(source_path: str | None) -> str:
    """Filename stem of a source path, without directories or extension."""
    return (source_path or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def compose_document_surface(
    document_id: str,
    doc: Document,
    embedding: list[float] | None = None,
) -> DocumentSurface:
    """Build a document's document-level row, split by provenance.

    Authored text -- the title and tags -- goes to ``matchable``, widened to
    the renderings a caller might type so it satisfies a match however the
    separators and word boundaries were written. Derived text -- the generated
    abstract, the source filename stem, and that stem's expansion -- goes to
    ``orienting``, where it ranks and orients but never satisfies a match.

    The stem's expansion sits with the stem rather than with the title: an
    expansion inherits the provenance of the text it expands, and a filename is
    an artifact of how the document arrived.

    Args:
        document_id: The document this row belongs to.
        doc: The stored document record supplying title, tags, abstract, and
            source path.
        embedding: Vector for the semantic arm. A caller that has one already
            -- the migration, carrying a legacy row's vector forward -- passes
            it; the indexing pipeline computes one from the composed text.

    Returns:
        The row to hand to the content store.
    """
    stem = source_path_stem(doc.source_path)
    tags_line = ", ".join(doc.tags or [])

    matchable = " ".join(
        part for part in (expand_for_index(doc.title or ""), expand_for_index(tags_line)) if part
    )
    orienting = " ".join(
        part for part in (doc.semantic_abstract or "", stem, expand_for_index(stem)) if part
    )

    return DocumentSurface(
        document_id=document_id,
        matchable=matchable,
        orienting=orienting,
        embedding=embedding,
        doc_type=doc.doc_type,
        lifecycle_status=doc.lifecycle_status,
        project=doc.project,
    )


def embedding_text(surface: DocumentSurface) -> str:
    """Text to embed for a document-level row.

    Both halves, because similarity is not matching: derived text exists to
    make a document findable and triageable, which is a ranking and
    orientation value the provenance rule preserves whole.
    """
    return f"{surface.matchable}\n\n{surface.orienting}"
