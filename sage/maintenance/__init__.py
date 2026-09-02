"""Out-of-band operator purge tooling (permanently out-of-band per CAS-ADR-029).

Command-line scripts that remove a document, a supersession chain, or an
ingest-window batch from a SAGE vault outside the normal API surface. Document
removal is excluded from the SAGE request surface by the No-Delete Invariant
(CAS-ADR-029), so these operations live here as operator-invoked CLIs rather than
as maintenance-surface MCP/REST tools. Nothing on the request surface (``sage.mcp_server``,
``sage.api``) may import this package; the import-topology test enforces it.
"""
