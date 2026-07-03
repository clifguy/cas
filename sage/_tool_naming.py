"""SAGE MCP tool naming: verb taxonomy and server-assignment oracle.

This module holds the durable naming invariants of the SAGE MCP tool
surface. None of it is consumed by production dispatch; the conformance
gates consume it to cross-check the live catalog:

- ``CANONICAL_VERBS`` — the verb categories that every SAGE MCP tool
  name must begin with (after stripping any compound prefix). Anchored
  to CAS-ADR-033.
- ``COMPOUND_PREFIXES`` — prefixes that compose with the verb taxonomy
  (currently ``bulk_`` and ``admin_``). Anchored to CAS-ADR-029.
- ``extract_verb`` — helper returning a tool name's canonical verb
  segment after stripping any compound prefix.
- ``SERVER_ASSIGNMENT`` — tool name → MCP server name (``sage`` or
  ``sage_admin``). The in-code transcription of the two-server split
  per CAS-ADR-034; the partition tests cross-check it against the
  prefix-derived registration.
"""

from __future__ import annotations

from typing import Final

# ----------------------------------------------------------------------
# Verb taxonomy (durable; anchored to CAS-ADR-033)
# ----------------------------------------------------------------------

#: Canonical verb categories that every SAGE MCP tool name must begin
#: with (after stripping any member of ``COMPOUND_PREFIXES``). Anchored
#: to CAS-ADR-033. The first segment of a tool name -- up to the first
#: underscore, or after consuming a compound prefix -- must be in this
#: set; the verb-compliance gate test enforces this.
CANONICAL_VERBS: Final[frozenset[str]] = frozenset(
    {
        # Read-spine verbs
        "get",
        "list",
        "search",
        "read",
        "traverse",
        "chain",
        # Mutation primitives
        "create",
        "update",
        "delete",
        # Boundary / derivation / validation
        "ingest",
        "recompute",
        "verify",
        # Admin-only outlier verbs (accepted on the sage_admin surface only).
        # `optimize` joins the precedent of `reload`/`migrate`: maintenance
        # operations on substrate state whose intent does not fit the
        # read-spine / mutation / derivation / validation taxonomy.
        "reload",
        "migrate",
        "optimize",
    }
)


#: Prefixes that compose with the verb taxonomy. After stripping a
#: compound prefix from the beginning of a tool name, the remainder
#: must begin with a canonical verb. ``bulk`` is retained for the
#: ``bulk_ingest_document`` tool whose rename to ``ingest_documents``
#: is deferred to a follow-on revision. ``admin`` is the prefix-
#: encodes-surface marker per CAS-ADR-029 v4.
COMPOUND_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "bulk",
        "admin",
    }
)


def extract_verb(tool_name: str) -> str:
    """Return the canonical verb segment of a tool name.

    Strips any leading compound prefix (e.g. ``bulk_``, ``admin_``),
    then returns the first underscore-separated segment of the
    remainder. Returns the empty string if ``tool_name`` is empty.

    Examples:
        ``extract_verb("get_document")`` returns ``"get"``.
        ``extract_verb("bulk_ingest_document")`` returns ``"ingest"``.
        ``extract_verb("admin_migrate_vault")`` returns ``"migrate"``.
        ``extract_verb("admin_recompute_views")`` returns ``"recompute"``.
    """
    if not tool_name:
        return ""
    head, _, tail = tool_name.partition("_")
    if head in COMPOUND_PREFIXES and tail:
        head, _, _ = tail.partition("_")
    return head


# ----------------------------------------------------------------------
# Server assignment (anchored to CAS-ADR-034)
# ----------------------------------------------------------------------

#: New tool name → MCP server name. The ``sage`` server hosts the
#: ordinary surface (read spine + mutation spine + record-collection
#: queries + per-document recomputes); the ``sage_admin`` server hosts
#: the maintenance surface (vault-state reads and writes + stack-state
#: reads + substrate maintenance). Server assignment is derivable from
#: the tool's prefix per CAS-ADR-029 v4's prefix-encodes-surface rule
#: (``admin_*`` → ``sage_admin``; everything else → ``sage``), but
#: codifying it here avoids re-deriving the partition at every
#: registration site and gives downstream consumers a single canonical
#: source.
#:
#: The two-server registration split (CAS-ADR-034) is live: the ``sage`` and
#: ``sage_admin`` stdio servers register their tools by deriving the server
#: from each tool name's first segment (``sage.mcp_server._surface_of``), so
#: this table is **not** consumed by registration. It serves as the
#: conformance oracle — the in-code transcription of the *SAGE MCP Tool
#: Surface* steering document's server registration map — against which the
#: partition tests cross-check the prefix-derived split.
SERVER_ASSIGNMENT: Final[dict[str, str]] = {
    # sage server (ordinary surface)
    "search": "sage",
    "get_document": "sage",
    "read_section": "sage",
    "read_projection": "sage",
    "list_headings": "sage",
    "traverse": "sage",
    "chain": "sage",
    "ingest_document": "sage",
    "create_edges": "sage",
    "delete_edge": "sage",
    "update_lifecycles": "sage",
    "update_metadata": "sage",
    "update_staging_edge": "sage",
    "list_staging_edges": "sage",
    "verify_preconditions": "sage",
    "verify_hashes": "sage",
    "list_pending_metadata": "sage",
    "get_filename_metadata": "sage",
    "list_directory": "sage",
    "bulk_ingest_document": "sage",
    "recompute_abstract": "sage",
    "recompute_pipeline": "sage",
    # sage_admin server (maintenance surface)
    "admin_list_vaults": "sage_admin",
    "admin_get_vault_config": "sage_admin",
    "admin_get_vault_stats": "sage_admin",
    "admin_get_stack_config": "sage_admin",
    "admin_create_vault": "sage_admin",
    "admin_reload_vault": "sage_admin",
    "admin_update_vault_config": "sage_admin",
    "admin_verify_vault_drift": "sage_admin",
    "admin_verify_vault_source_files": "sage_admin",
    "admin_migrate_vault": "sage_admin",
    "admin_recompute_views": "sage_admin",
    "admin_recompute_deferred_vault_abstracts": "sage_admin",
    "admin_optimize_vault_content_store": "sage_admin",
}


# ----------------------------------------------------------------------
# Module-level invariants (checked at import time)
# ----------------------------------------------------------------------

# This assertion is an init-time table-consistency guard; it fires at
# import so the failure mode is impossible to miss. Production code paths
# do not rely on assertion truth values, so the ruff S101 ban on
# `assert` does not apply.

# SERVER_ASSIGNMENT values must be one of the two allowed server names.
_unknown_servers = set(SERVER_ASSIGNMENT.values()) - {"sage", "sage_admin"}
assert _unknown_servers == set(), (  # noqa: S101
    f"SERVER_ASSIGNMENT values must be 'sage' or 'sage_admin': {_unknown_servers}"
)
