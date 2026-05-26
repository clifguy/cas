"""Shared date-parsing helpers for document_date round-tripping.

Consolidates the formerly-parallel ``_parse_document_date`` (retrieval.py)
and ``_parse_doc_date`` (graph_ops.py) helpers.
"""

from datetime import datetime, timezone


def parse_document_date(date_str: str | None) -> datetime | None:
    """Parse a document_date string into a UTC datetime, or None.

    Accepts the contract YYYY-MM-DD shape plus any other ISO-8601 form
    ``datetime.fromisoformat`` understands (including the trailing Z that
    ingest paths have historically produced for some records). Naive
    results are treated as UTC; aware results are normalized to UTC.
    Returns None on falsy input and on parse failure, since the read path
    should not crash on out-of-spec data already persisted upstream.
    """
    if not date_str:
        return None
    try:
        parsed = datetime.fromisoformat(date_str)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
