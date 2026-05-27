"""SAGE MCP tool rename mapping and verb taxonomy.

This module holds the four data structures that drive the verb-convention
rename and inner-prefix simplification of every SAGE MCP tool name:

- ``RENAME_MAPPING`` — current name → new name. Consumed by the
  alias-resolution middleware to rewrite old-name MCP calls onto their
  current handlers during the deprecation window.
- ``REMOVED_TOOLS`` — old names that were dropped from the MCP surface
  entirely (no rename target). The alias middleware returns an
  explanatory error when these are invoked.
- ``CANONICAL_VERBS`` — the verb categories that every SAGE MCP tool
  name must begin with (after stripping any compound prefix). Anchored
  to CAS-ADR-033.
- ``COMPOUND_PREFIXES`` — prefixes that compose with the verb taxonomy
  (currently just ``bulk_``). Anchored to CAS-ADR-029.
- ``SERVER_ASSIGNMENT`` — new tool name → MCP server name (``sage`` or
  ``sage_admin``). Consumed by the two-server registration split per
  CAS-ADR-034.

The deprecation window is scheduled to close 2026-06-15. The alias
middleware and the ``RENAME_MAPPING`` / ``REMOVED_TOOLS`` entries are
expected to be removed in a follow-up change after that date; the verb
taxonomy and ``SERVER_ASSIGNMENT`` are durable.
"""

from __future__ import annotations

from typing import Final

# ----------------------------------------------------------------------
# Alias mapping
# ----------------------------------------------------------------------

#: Mapping of pre-rename tool names to their current (renamed) targets.
#: The alias middleware consults this table on every ``call_tool`` invocation
#: and rewrites the ``name`` argument to the value side of the mapping
#: before dispatching. Old-name invocations emit a deprecation log entry
#: naming both the old and new names; see the alias middleware module for
#: the rewrite point.
RENAME_MAPPING: Final[dict[str, str]] = {
    # Read-spine tools (sage server)
    "sage_discover": "search",
    "sage_get_document": "get_document",
    "sage_read_section": "read_section",
    "sage_read_projection": "read_projection",
    "sage_list_headings": "list_headings",
    "sage_list_vaults": "list_vaults",
    "sage_get_vault_config": "get_vault_config",
    "sage_vault_stats": "get_vault_stats",
    "sage_traverse": "traverse",
    "sage_chain": "chain",
    # Mutation spine (sage server)
    "sage_ingest": "ingest_document",
    "sage_link": "create_edge",
    "sage_unlink": "delete_edge",
    "sage_set_lifecycle": "update_lifecycle",
    "sage_update_metadata": "update_metadata",
    "sage_update_staging_edge": "update_staging_edge",
    "sage_list_staging_edges": "list_staging_edges",
    "sage_check_preconditions": "verify_preconditions",
    "sage_pending_metadata": "list_pending_metadata",
    "sage_parse_filename": "get_filename_metadata",
    # Edge-level bulk (sage server per CAS-ADR-029)
    "sage_bulk_link": "bulk_create_edge",
    # App-tools (server assignment varies per CAS-ADR-034)
    "app_scan_directory": "list_directory",
    "app_batch_ingest": "bulk_ingest_document",
    # Document-level bulk (sage_admin server per CAS-ADR-029)
    "sage_bulk_set_lifecycle": "bulk_update_lifecycle",
    "sage_bulk_update_metadata": "bulk_update_metadata",
    # Maintenance / admin surface (sage_admin server)
    "sage_reabstract": "recompute_abstract",
    "sage_recompute_pipeline": "recompute_pipeline",
    "sage_refresh_views": "recompute_views",
    "sage_hash_check": "verify_hash",
    "sage_admin_detect_drift": "verify_vault_drift",
    "sage_admin_migrate_vault": "migrate_vault",
    "sage_admin_reabstract_deferred_vault": "recompute_deferred_vault_abstracts",
    "sage_admin_optimize_vault": "optimize_vault_content_store",
    "sage_reload_vault": "reload_vault",
    "sage_create_vault": "create_vault",
    "sage_update_vault_config": "update_vault_config",
    "sage_get_stack_config": "get_stack_config",
}


#: Old tool names that were dropped from the MCP surface entirely. These
#: have no rename target; the alias middleware returns an explanatory
#: error pointing to the replacement mechanism (per the SAGE MCP Tool
#: Surface enumeration discipline established by CAS-ADR-035).
#:
#: All entries are informational at code load time: every name listed
#: here is already absent from the live tool registry. The set documents
#: the removal decision for surfaces (skills, memory, vault docs) that
#: still reference the dropped names.
REMOVED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        # Removed: no MCP caller story. The HTTP endpoint and service layer
        # are retained.
        "sage_register_user",
        # Removed: folded into ``read_projection(write_to_path=...)``.
        "sage_export_projection",
        # Removed: collapsed into ``update_staging_edge`` with a discriminating
        # ``action`` argument (``"confirm"`` or ``"dismiss"``).
        "sage_confirm_staging_edge",
        "sage_dismiss_staging_edge",
    }
)


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
#: must begin with a canonical verb. Currently the only compound
#: prefix is ``bulk`` per CAS-ADR-029.
COMPOUND_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "bulk",
    }
)


def extract_verb(tool_name: str) -> str:
    """Return the canonical verb segment of a tool name.

    Strips any leading compound prefix (e.g. ``bulk_``), then returns
    the first underscore-separated segment of the remainder. Returns
    the empty string if ``tool_name`` is empty.

    Examples:
        ``extract_verb("get_document")`` returns ``"get"``.
        ``extract_verb("bulk_create_edge")`` returns ``"create"``.
        ``extract_verb("migrate_vault")`` returns ``"migrate"``.
        ``extract_verb("recompute_deferred_vault_abstracts")`` returns ``"recompute"``.
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
#: ordinary surface (read spine + edge-level mutation); the
#: ``sage_admin`` server hosts the maintenance surface (admin-only
#: tools + document-level bulk mutation + vault-config writes +
#: substrate maintenance). Server assignment is derivable from the
#: tool's verb / prefix per CAS-ADR-029 (``admin_*`` was historically
#: bijective with ``sage_admin``; ``bulk_*`` is split by noun), but
#: codifying it here avoids re-deriving the partition at every
#: registration site and gives downstream consumers a single canonical
#: source.
#:
#: Until the two-server registration split lands, every tool registers
#: on the ``sage`` server regardless of the value here.
SERVER_ASSIGNMENT: Final[dict[str, str]] = {
    # sage server
    "search": "sage",
    "get_document": "sage",
    "read_section": "sage",
    "read_projection": "sage",
    "list_headings": "sage",
    "list_vaults": "sage",
    "get_vault_config": "sage",
    "get_vault_stats": "sage",
    "traverse": "sage",
    "chain": "sage",
    "ingest_document": "sage",
    "create_edge": "sage",
    "delete_edge": "sage",
    "update_lifecycle": "sage",
    "update_metadata": "sage",
    "update_staging_edge": "sage",
    "list_staging_edges": "sage",
    "verify_preconditions": "sage",
    "list_pending_metadata": "sage",
    "get_filename_metadata": "sage",
    "bulk_create_edge": "sage",
    "list_directory": "sage",
    # sage_admin server
    "bulk_update_lifecycle": "sage_admin",
    "bulk_update_metadata": "sage_admin",
    "bulk_ingest_document": "sage_admin",
    "recompute_abstract": "sage_admin",
    "recompute_pipeline": "sage_admin",
    "recompute_views": "sage_admin",
    "verify_hash": "sage_admin",
    "verify_vault_drift": "sage_admin",
    "migrate_vault": "sage_admin",
    "recompute_deferred_vault_abstracts": "sage_admin",
    "optimize_vault_content_store": "sage_admin",
    "reload_vault": "sage_admin",
    "create_vault": "sage_admin",
    "update_vault_config": "sage_admin",
    "get_stack_config": "sage_admin",
}


# ----------------------------------------------------------------------
# Module-level invariants (checked at import time)
# ----------------------------------------------------------------------

# These assertions are init-time table-consistency guards; they fire
# only when the four module constants drift relative to one another,
# and they fire at import so the failure mode is impossible to miss.
# Production code paths do not rely on assertion truth values, so the
# ruff S101 ban on `assert` does not apply.

_rename_targets = set(RENAME_MAPPING.values())
_assigned_names = set(SERVER_ASSIGNMENT.keys())
_missing_assignments = _rename_targets - _assigned_names
_orphan_assignments = _assigned_names - _rename_targets
assert _missing_assignments == set() and _orphan_assignments == set(), (  # noqa: S101
    "RENAME_MAPPING values must match SERVER_ASSIGNMENT keys exactly: "
    f"missing: {_missing_assignments}; orphans: {_orphan_assignments}"
)

# REMOVED_TOOLS and RENAME_MAPPING must be disjoint (a name is either
# renamed or removed, never both).
assert REMOVED_TOOLS.isdisjoint(RENAME_MAPPING.keys()), (  # noqa: S101
    f"REMOVED_TOOLS and RENAME_MAPPING keys overlap: {REMOVED_TOOLS & RENAME_MAPPING.keys()}"
)

# SERVER_ASSIGNMENT values must be one of the two allowed server names.
_unknown_servers = set(SERVER_ASSIGNMENT.values()) - {"sage", "sage_admin"}
assert _unknown_servers == set(), (  # noqa: S101
    f"SERVER_ASSIGNMENT values must be 'sage' or 'sage_admin': {_unknown_servers}"
)
