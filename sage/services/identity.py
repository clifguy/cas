"""Document ID generation per BH-001 through BH-003.

ID format: Hash(source_path + created_at) truncated to 8 hex + slugified title.
Pattern: ^[0-9a-f]{8}_[a-z0-9_]+$
"""

import hashlib
import re
import unicodedata

# Canonical document-id shape: 8 hex chars, an underscore, then a slug.
# Mirrors the validator pattern enforced on DocumentIdStr in
# sage/models/schemas.py; kept here as the single source for the
# generator and the shape predicate so the two never drift.
_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def is_well_formed_document_id(value: str) -> bool:
    """Return whether ``value`` matches the canonical document-id shape.

    A well-formed id is 8 hex chars, an underscore, then a slug of
    lowercase alphanumerics and underscores. The check is purely lexical:
    it says nothing about whether a document with that id exists, only
    whether the string could ever have been minted by this vault's id
    generator. Used by the not-found diagnostics to tell a malformed or
    cross-vault identifier apart from a plausibly-real one (CAS-ADR-039).
    """
    return _DOCUMENT_ID_RE.fullmatch(value) is not None


def document_id_slug(value: str) -> str | None:
    """Return the slug suffix of a well-formed document id, else ``None``.

    The slug is the portion after the leading ``<8 hex>_`` hash prefix.
    Two ids minted from the same title share a slug even when their hash
    prefixes differ, so comparing slugs surfaces the "same document,
    different version/hash" case the not-found diagnostics report as
    ``slug_matches_catalog`` (CAS-ADR-039). Returns ``None`` for ids that
    are not well-formed, since their slug is undefined.
    """
    if not is_well_formed_document_id(value):
        return None
    return value.split("_", 1)[1]


def generate_document_id(source_path: str, created_at: str, title: str) -> str:
    """Generate a human-readable document ID.

    Args:
        source_path: Path to source file (e.g., "reports/doc_a.docx").
        created_at: ISO 8601 timestamp string.
        title: Document title for the slug component.

    Returns:
        ID matching pattern ^[0-9a-f]{8}_[a-z0-9_]+$
    """
    hash_input = f"{source_path}{created_at}".encode("utf-8")
    hash_hex = hashlib.sha256(hash_input).hexdigest()[:8]
    slug = _slugify(title)
    return f"{hash_hex}_{slug}"


def _slugify(title: str) -> str:
    """Convert title to lowercase alphanumeric + underscores."""
    title = unicodedata.normalize("NFKD", title).lower()
    title = re.sub(r"[^a-z0-9]", "_", title)
    title = re.sub(r"_+", "_", title)
    title = title.strip("_")
    return title or "untitled"
