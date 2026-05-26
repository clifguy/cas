"""Document ID generation per BH-001 through BH-003.

ID format: Hash(source_path + created_at) truncated to 8 hex + slugified title.
Pattern: ^[0-9a-f]{8}_[a-z0-9_]+$
"""

import hashlib
import re
import unicodedata


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
