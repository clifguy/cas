"""SAGE MCP tool naming: verb taxonomy and server-assignment oracle.

This module holds the durable naming invariants of the SAGE MCP tool
surface. Apart from the alias mapping, none of it is consumed by
production dispatch; the conformance gates consume it to cross-check
the live catalog:

- ``CANONICAL_VERBS`` — the verb categories that every SAGE MCP tool
  name must begin with (after stripping any compound prefix). Anchored
  to CAS-ADR-033.
- ``COMPOUND_PREFIXES`` — prefixes that compose with the verb taxonomy
  (currently ``bulk_`` and ``admin_``). Anchored to CAS-ADR-029.
- ``extract_verb`` — helper returning a tool name's canonical verb
  segment after stripping any compound prefix.
- ``SERVER_ASSIGNMENT`` — tool name → MCP server name (``sage`` or
  ``sage_maint``). The in-code transcription of the two-server split
  per CAS-ADR-034; the partition tests cross-check it against the
  prefix-derived registration.
- ``MAINT_ALIAS_MAPPING`` — pre-rename ``admin_*`` name → canonical
  ``maint_*`` name. The one exception to "none of it is consumed by
  production dispatch": the alias middleware consults it on every
  ``call_tool`` invocation so pre-rename callers keep working.
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
        # Maintenance-only outlier verbs (accepted on the sage_maint surface
        # only): maintenance operations on substrate state whose intent does
        # not fit the read-spine / mutation / derivation / validation taxonomy.
        # This is the set the gate enforces; the argument admitting each member
        # lives in the *SAGE MCP Tool Surface* steering document, which holds
        # the population of every partition CAS-ADR-033 establishes -- the same
        # document the server registration map below transcribes.
        "reload",
        "migrate",
        "optimize",
        "restore",
    }
)


#: Prefixes that compose with the verb taxonomy. After stripping a
#: compound prefix from the beginning of a tool name, the remainder
#: must begin with a canonical verb. ``bulk`` is retained for the
#: ``bulk_ingest_document`` tool whose rename to ``ingest_documents``
#: is deferred to a follow-on revision. ``maint`` is the prefix-
#: encodes-surface marker per CAS-ADR-029 v4 (the surface's pre-rename
#: ``admin`` prefix survives only in the dispatch-level alias mapping
#: below, never in a registered name, so it composes with nothing).
COMPOUND_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "bulk",
        "maint",
    }
)


def extract_verb(tool_name: str) -> str:
    """Return the canonical verb segment of a tool name.

    Strips any leading compound prefix (e.g. ``bulk_``, ``maint_``),
    then returns the first underscore-separated segment of the
    remainder. Returns the empty string if ``tool_name`` is empty.

    Examples:
        ``extract_verb("get_document")`` returns ``"get"``.
        ``extract_verb("bulk_ingest_document")`` returns ``"ingest"``.
        ``extract_verb("maint_migrate_vault")`` returns ``"migrate"``.
        ``extract_verb("maint_recompute_views")`` returns ``"recompute"``.
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
#: queries + per-document recomputes); the ``sage_maint`` server hosts
#: the maintenance surface (vault-state reads and writes + stack-state
#: reads + substrate maintenance). Server assignment is derivable from
#: the tool's prefix per CAS-ADR-029 v4's prefix-encodes-surface rule
#: (``maint_*`` → ``sage_maint``; everything else → ``sage``), but
#: codifying it here avoids re-deriving the partition at every
#: registration site and gives downstream consumers a single canonical
#: source.
#:
#: The two-surface registration split (CAS-ADR-034) is live: the ``sage``
#: and ``sage_maint`` surfaces register their tools by deriving the surface
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
    # sage_maint server (maintenance surface)
    "maint_list_vaults": "sage_maint",
    "maint_get_vault_config": "sage_maint",
    "maint_get_vault_stats": "sage_maint",
    "maint_get_stack_config": "sage_maint",
    "maint_create_vault": "sage_maint",
    "maint_reload_vault": "sage_maint",
    "maint_update_vault_config": "sage_maint",
    "maint_verify_vault_drift": "sage_maint",
    "maint_verify_vault_source_files": "sage_maint",
    "maint_restore_vault_source_file": "sage_maint",
    "maint_migrate_vault": "sage_maint",
    "maint_recompute_views": "sage_maint",
    "maint_recompute_deferred_vault_abstracts": "sage_maint",
    "maint_optimize_vault_content_store": "sage_maint",
}


# ----------------------------------------------------------------------
# Maintenance-surface alias mapping (anchored to CAS-ADR-034)
# ----------------------------------------------------------------------

#: Pre-rename maintenance tool name → canonical name. The surface's
#: tools were renamed ``admin_*`` → ``maint_*`` because the old prefix
#: implied a privilege boundary the surface does not carry (CAS-ADR-034:
#: uniform auth across both surfaces; the split exists only to keep the
#: ordinary catalog small). The alias middleware in
#: ``sage.mcp_server._LoggingFastMCP.call_tool`` consults this table on
#: every invocation and rewrites an old name onto its canonical target,
#: so pre-rename callers keep working indefinitely — no removal is
#: scheduled. The old names are deliberately **not** registered as
#: tools: the advertised catalog carries only the canonical names.
#:
#: The table is written out rather than derived from
#: ``SERVER_ASSIGNMENT`` on purpose: it covers exactly the tools that
#: existed under the old prefix. A maintenance tool added after the
#: rename gets no fabricated ``admin_*`` alias.
MAINT_ALIAS_MAPPING: Final[dict[str, str]] = {
    "admin_list_vaults": "maint_list_vaults",
    "admin_get_vault_config": "maint_get_vault_config",
    "admin_get_vault_stats": "maint_get_vault_stats",
    "admin_get_stack_config": "maint_get_stack_config",
    "admin_create_vault": "maint_create_vault",
    "admin_reload_vault": "maint_reload_vault",
    "admin_update_vault_config": "maint_update_vault_config",
    "admin_verify_vault_drift": "maint_verify_vault_drift",
    "admin_verify_vault_source_files": "maint_verify_vault_source_files",
    "admin_migrate_vault": "maint_migrate_vault",
    "admin_recompute_views": "maint_recompute_views",
    "admin_recompute_deferred_vault_abstracts": "maint_recompute_deferred_vault_abstracts",
    "admin_optimize_vault_content_store": "maint_optimize_vault_content_store",
}


# ----------------------------------------------------------------------
# Module-level invariants (checked at import time)
# ----------------------------------------------------------------------

# This assertion is an init-time table-consistency guard; it fires at
# import so the failure mode is impossible to miss. Production code paths
# do not rely on assertion truth values, so the ruff S101 ban on
# `assert` does not apply.

# SERVER_ASSIGNMENT values must be one of the two allowed server names.
_unknown_servers = set(SERVER_ASSIGNMENT.values()) - {"sage", "sage_maint"}
assert _unknown_servers == set(), (  # noqa: S101
    f"SERVER_ASSIGNMENT values must be 'sage' or 'sage_maint': {_unknown_servers}"
)

# Every alias must target a registered maintenance-surface name, and no
# alias may collide with a registered name (a collision would shadow a
# live tool behind the rewrite).
_orphan_aliases = {
    old for old, new in MAINT_ALIAS_MAPPING.items() if SERVER_ASSIGNMENT.get(new) != "sage_maint"
}
assert _orphan_aliases == set(), (  # noqa: S101
    f"alias target(s) not on the maintenance surface: {_orphan_aliases}"
)
_alias_collisions = set(MAINT_ALIAS_MAPPING) & set(SERVER_ASSIGNMENT)
assert _alias_collisions == set(), (  # noqa: S101
    f"alias name(s) collide with registered tool names: {_alias_collisions}"
)
