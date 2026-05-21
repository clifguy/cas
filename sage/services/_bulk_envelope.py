"""Per-item error envelope helper for sage_bulk_* operations (CAS-ADR-029).

The bulk-document operations defined by CAS-ADR-029 surface per-item
SAGEErrors as nested error envelopes inside their response body rather
than as batch-level 4xx responses. This module owns the translation from
SAGEError to that envelope shape so each bulk service method can apply
it without depending on the MCP layer (boundary rule).
"""

from sage.api.errors import SAGEError


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
