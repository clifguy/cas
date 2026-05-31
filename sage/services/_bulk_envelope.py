"""Per-item error envelope helper for sage_bulk_* operations (CAS-ADR-029).

The bulk-document operations defined by CAS-ADR-029 surface per-item
SAGEErrors as nested error envelopes inside their response body rather
than as batch-level 4xx responses. This module owns the translation from
SAGEError to that envelope shape so each bulk service method can apply
it without depending on the MCP layer (boundary rule).
"""

from sage.api.errors import (
    AmbiguousDocumentIdentifierError,
    MissingDocumentIdentifierError,
    SAGEError,
)


def resolve_item_document_id(document_id: str | None, doc_id: str | None, *, tool: str) -> str:
    """Resolve a per-item document identifier from the canonical
    ``document_id`` and its back-compatible ``doc_id`` alias.

    Exactly one must be supplied. Both present (even with equal values) is
    ambiguous; neither is missing. The raised errors are ``SAGEError``
    subclasses, so a bulk service loop's ``except SAGEError`` turns them
    into per-item error envelopes -- the resolution failure stays scoped to
    the offending item rather than aborting the batch. Mirrors the
    read-tool ``document_id``/``doc_id`` resolution.
    """
    if document_id is not None and doc_id is not None:
        raise AmbiguousDocumentIdentifierError(tool=tool, canonical="document_id", alias="doc_id")
    if document_id is None and doc_id is None:
        raise MissingDocumentIdentifierError(tool=tool, accepted=["document_id", "doc_id"])
    return document_id if document_id is not None else doc_id


def sage_error_to_envelope(exc: SAGEError) -> dict:
    """Translate a SAGEError into the same envelope shape the MCP layer emits.

    Mirrors ``mcp_server._error_response`` so per-item errors in a bulk
    response are indistinguishable from single-item MCP errors at the
    caller.
    """
    payload: dict = {"error": exc.code, "message": exc.message}
    if exc.detail:
        payload["detail"] = exc.detail
    return payload
