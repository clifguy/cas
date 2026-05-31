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
  (currently ``bulk_`` and ``admin_``). Anchored to CAS-ADR-029.
- ``SERVER_ASSIGNMENT`` — new tool name → MCP server name (``sage`` or
  ``sage_admin``). Consumed by the two-server registration split per
  CAS-ADR-034.

The alias middleware does a single-step lookup, not transitive
resolution. When a tool is renamed twice (once by a prior pass, once
by a subsequent pass), the prior entry's value is updated to the
current target so the middleware still resolves old names correctly.

The deprecation window for the combined CAS-ADR-029 / CAS-ADR-029 rename aliases
is scheduled to close 2026-06-15. The alias middleware and the
``RENAME_MAPPING`` / ``REMOVED_TOOLS`` entries are expected to be
removed in a follow-up change after that date; the verb taxonomy and
``SERVER_ASSIGNMENT`` are durable.
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
    # ------------------------------------------------------------------
    # Pre-CAS-ADR-029 aliases (sage_* / app_* prefixes).
    # Values are updated to the post-CAS-ADR-029 targets so the single-step
    # alias resolution still reaches a live tool name.
    # ------------------------------------------------------------------
    # Read-spine tools (sage server)
    "sage_discover": "search",
    "sage_get_document": "get_document",
    "sage_read_section": "read_section",
    "sage_read_projection": "read_projection",
    "sage_list_headings": "list_headings",
    "sage_list_vaults": "admin_list_vaults",
    "sage_get_vault_config": "admin_get_vault_config",
    "sage_vault_stats": "admin_get_vault_stats",
    "sage_traverse": "traverse",
    "sage_chain": "chain",
    # Mutation spine (sage server)
    "sage_ingest": "ingest_document",
    "sage_link": "create_edges",
    "sage_unlink": "delete_edge",
    "sage_set_lifecycle": "update_lifecycles",
    "sage_update_metadata": "update_metadata",
    "sage_update_staging_edge": "update_staging_edge",
    "sage_list_staging_edges": "list_staging_edges",
    "sage_check_preconditions": "verify_preconditions",
    "sage_pending_metadata": "list_pending_metadata",
    "sage_parse_filename": "get_filename_metadata",
    # Edge-level bulk (sage server per CAS-ADR-029)
    "sage_bulk_link": "create_edges",
    # App-tools (server assignment varies per CAS-ADR-034)
    "app_scan_directory": "list_directory",
    "app_batch_ingest": "bulk_ingest_document",
    # Document-level bulk (collapsed under CAS-ADR-029 v4 plural-noun)
    "sage_bulk_set_lifecycle": "update_lifecycles",
    "sage_bulk_update_metadata": "update_metadata",
    # Maintenance / admin surface (sage_admin server)
    "sage_reabstract": "recompute_abstract",
    "sage_recompute_pipeline": "recompute_pipeline",
    "sage_refresh_views": "admin_recompute_views",
    "sage_hash_check": "verify_hashes",
    "sage_admin_detect_drift": "admin_verify_vault_drift",
    "sage_admin_migrate_vault": "admin_migrate_vault",
    "sage_admin_reabstract_deferred_vault": "admin_recompute_deferred_vault_abstracts",
    "sage_admin_optimize_vault": "admin_optimize_vault_content_store",
    "sage_reload_vault": "admin_reload_vault",
    "sage_create_vault": "admin_create_vault",
    "sage_update_vault_config": "admin_update_vault_config",
    "sage_get_stack_config": "admin_get_stack_config",
    # ------------------------------------------------------------------
    # CAS-ADR-029 pass: plural-noun convention + admin_ prefix-encodes-surface
    # rule per CAS-ADR-029 v4, with the five v7 amendments to the
    # SAGE MCP Tool Surface steering doc's orphan-mappings table.
    # ------------------------------------------------------------------
    # Pair collapses to plural-noun on sage
    "create_edge": "create_edges",
    "bulk_create_edge": "create_edges",
    "update_lifecycle": "update_lifecycles",
    "bulk_update_lifecycle": "update_lifecycles",
    "bulk_update_metadata": "update_metadata",
    # Admin prefix added per CAS-ADR-029 v4 (v6-firm renames)
    "verify_vault_drift": "admin_verify_vault_drift",
    "migrate_vault": "admin_migrate_vault",
    "recompute_deferred_vault_abstracts": "admin_recompute_deferred_vault_abstracts",
    "create_vault": "admin_create_vault",
    "reload_vault": "admin_reload_vault",
    "update_vault_config": "admin_update_vault_config",
    "recompute_views": "admin_recompute_views",
    "get_stack_config": "admin_get_stack_config",
    # v6 pending-classification resolved
    "optimize_vault_content_store": "admin_optimize_vault_content_store",
    "verify_vault_source_files": "admin_verify_vault_source_files",
    # v7 amendments: scope rule re-applied; admin_ tracks vault/stack scope
    "list_vaults": "admin_list_vaults",
    "get_vault_config": "admin_get_vault_config",
    "get_vault_stats": "admin_get_vault_stats",
    # v7 amendment: record-collection scope; plural-noun rename, moves
    # off the maintenance surface
    "verify_hash": "verify_hashes",
    # v7 amendment: per-document scope; no rename, but the prior
    # admin_recompute_abstract mapping needs to point to the unprefixed
    # name. There is no current-deployed admin_recompute_abstract on
    # the live surface, so no rename entry is needed for the v7 move;
    # the only effect is in SERVER_ASSIGNMENT below.
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
