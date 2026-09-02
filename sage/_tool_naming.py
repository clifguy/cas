"""SAGE MCP tool naming: verb taxonomy, surface assignment, and aliases.

This module holds the durable naming invariants of the SAGE MCP tool
surface and the one table that decides where each tool registers:

- ``CANONICAL_VERBS`` — the verb categories that every SAGE MCP tool
  name must begin with (after stripping any compound prefix). Anchored
  to CAS-ADR-033. Consumed by the conformance gates only.
- ``MAINT_ONLY_VERBS`` — the closed class of outlier verbs whose tools
  register on the maintenance surface only. Anchored to CAS-ADR-033. The
  restriction is carried by ``SERVER_ASSIGNMENT`` and enforced by the
  import-time invariants below, not by any tool-name prefix.
- ``COMPOUND_PREFIXES`` — prefixes that compose with the verb taxonomy
  (currently ``bulk_`` and ``maint_``). Anchored to CAS-ADR-029.
  Consumed by the conformance gates only.
- ``extract_verb`` — helper returning a tool name's canonical verb
  segment after stripping any compound prefix.
- ``SERVER_ASSIGNMENT`` — tool name → MCP server name (``sage`` or
  ``sage_maint``). The registration authority for the two-server split
  per CAS-ADR-034: ``build_partitioned_server`` reads it to decide which
  tools each surface carries, and a registered tool with no row here
  fails registration rather than landing on a default surface.
- ``PREFIX_SURFACE_DIVERGENCES`` — the tools whose surface deliberately
  differs from what their name prefix would suggest. The ``maint_``
  prefix is a naming convention (CAS-ADR-029), not a registration rule;
  this set records every place the two are allowed to disagree so the
  partition gates can hold them to exactly that.
- ``MAINT_ALIAS_MAPPING`` — pre-rename ``admin_*`` name → canonical
  ``maint_*`` name. The alias middleware consults it on every
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
        # Maintenance-only outlier verbs; see MAINT_ONLY_VERBS below.
        "reload",
        "migrate",
        "optimize",
        "restore",
    }
)


#: The maintenance-only outlier verbs (CAS-ADR-033): maintenance operations
#: on substrate state whose intent does not fit the read-spine / mutation /
#: derivation / validation taxonomy. Each is accepted as a member of a
#: closed class rather than promoted to a category of its own. A tool
#: carrying one of these verbs registers on the maintenance surface only --
#: a restriction ``SERVER_ASSIGNMENT`` carries and ``_check_table_invariants``
#: enforces at import, not a property of the verb taxonomy or of any
#: tool-name prefix. The argument admitting each member lives in the *SAGE
#: MCP Tool Surface* steering document, which holds the population of every
#: partition CAS-ADR-033 establishes -- the same document the server
#: registration map below transcribes.
MAINT_ONLY_VERBS: Final[frozenset[str]] = frozenset({"reload", "migrate", "optimize", "restore"})


#: Prefixes that compose with the verb taxonomy. After stripping a
#: compound prefix from the beginning of a tool name, the remainder
#: must begin with a canonical verb. ``bulk`` is retained for the
#: ``bulk_ingest_document`` tool whose rename to ``ingest_documents``
#: is deferred to a follow-on revision. ``maint`` is the naming marker
#: for maintenance tools per CAS-ADR-029 (the surface's pre-rename
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

#: Tool name → MCP server name. The ``sage`` server hosts the ordinary
#: surface (read spine + mutation spine + record-collection queries +
#: per-document recomputes, plus the one tool that enumerates the vaults
#: every other ordinary tool is addressed to); the ``sage_maint`` server
#: hosts the maintenance surface (vault-state reads and writes +
#: stack-state reads + substrate maintenance).
#:
#: This table is the registration authority for the two-surface split
#: (CAS-ADR-034): ``sage.mcp_server.build_partitioned_server`` keeps, on
#: each surface, exactly the tools this table assigns to it, and refuses
#: to build a surface when a registered tool has no row here. It is the
#: in-code transcription of the *SAGE MCP Tool Surface* steering
#: document's server registration map; moving a tool between surfaces
#: is an edit to this row and to that map, never a rename.
#:
#: The ``maint_`` prefix is the naming convention for maintenance tools
#: (CAS-ADR-029); it does not decide registration. The partition gates
#: cross-check the two, holding their disagreements to exactly
#: ``PREFIX_SURFACE_DIVERGENCES`` below.
SERVER_ASSIGNMENT: Final[dict[str, str]] = {
    # sage server (ordinary surface)
    # Vault enumeration: the ordinary surface is vault-addressed, so it
    # carries the one tool that lists the values its ``vault_id``
    # arguments range over. The name keeps its maintenance prefix -- the
    # move and any rename are decisions on independent cadences -- and
    # the divergence is recorded in PREFIX_SURFACE_DIVERGENCES.
    "maint_list_vaults": "sage",
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


#: Tools whose assigned surface differs from the one their name prefix
#: suggests (``maint_*`` → ``sage_maint``; everything else → ``sage``).
#:
#: Written out by hand, not derived from ``SERVER_ASSIGNMENT``: the
#: partition gates compare the prefix convention against the table and
#: require their disagreements to equal this set exactly, in both
#: directions, and pin its population. A tool joins this set only by a
#: recorded decision, so it cannot quietly grow into an allowlist.
#:
#: Vault enumeration is the one member: it carries the maintenance prefix
#: because it reads the SAGE process's vault registry rather than records
#: within a vault, and it registers on the ordinary surface because that
#: surface is vault-addressed and otherwise cannot enumerate the vaults
#: its own ``vault_id`` arguments range over.
PREFIX_SURFACE_DIVERGENCES: Final[frozenset[str]] = frozenset({"maint_list_vaults"})


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
#: rename gets no fabricated ``admin_*`` alias, and a tool that has since
#: moved to the ordinary surface keeps the alias it already had -- the
#: alias follows the name, and resolves on whichever surface registers
#: the target (CAS-ADR-034: an alias grants nothing the canonical name
#: does not).
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

# These assertions are an init-time table-consistency guard; they fire at
# import so the failure mode is impossible to miss. Production code paths
# do not rely on assertion truth values, so the ruff S101 ban on
# `assert` does not apply.


def _check_table_invariants(
    assignment: dict[str, str],
    aliases: dict[str, str],
    divergences: frozenset[str],
) -> None:
    """Assert the cross-table invariants; called once at import.

    Factored so a test can exercise the checks against a deliberately
    broken copy without re-importing the module.
    """
    # SERVER_ASSIGNMENT values must be one of the two allowed server names.
    unknown_servers = set(assignment.values()) - {"sage", "sage_maint"}
    assert unknown_servers == set(), (  # noqa: S101
        f"SERVER_ASSIGNMENT values must be 'sage' or 'sage_maint': {unknown_servers}"
    )

    # Every declared divergence must name a registered tool; a typo or a
    # stale entry would otherwise import cleanly and surface only in tests.
    unknown_divergences = divergences - set(assignment)
    assert unknown_divergences == set(), (  # noqa: S101
        f"PREFIX_SURFACE_DIVERGENCES names unregistered tool(s): {unknown_divergences}"
    )

    # Every alias must target a registered name (on either surface -- the
    # alias follows the name, not the surface), and no alias may collide
    # with a registered name (a collision would shadow a live tool behind
    # the rewrite).
    orphan_aliases = {old for old, new in aliases.items() if new not in assignment}
    assert orphan_aliases == set(), (  # noqa: S101
        f"alias(es) whose target is not a registered tool: {orphan_aliases}"
    )
    alias_collisions = set(aliases) & set(assignment)
    assert alias_collisions == set(), (  # noqa: S101
        f"alias name(s) collide with registered tool names: {alias_collisions}"
    )

    # The maintenance-only outlier verbs are scoped by this table: a tool
    # whose verb is one of them registers on the maintenance surface and
    # nowhere else. The verb taxonomy states the class; the table places
    # its members, and no tool-name prefix is consulted.
    assert MAINT_ONLY_VERBS <= CANONICAL_VERBS, (  # noqa: S101
        f"MAINT_ONLY_VERBS is not a subset of CANONICAL_VERBS: "
        f"{sorted(MAINT_ONLY_VERBS - CANONICAL_VERBS)}"
    )
    misplaced = {
        name: extract_verb(name)
        for name, server in assignment.items()
        if extract_verb(name) in MAINT_ONLY_VERBS and server != "sage_maint"
    }
    assert misplaced == {}, (  # noqa: S101
        "tool(s) carrying a maintenance-only verb registered off the "
        f"maintenance surface (tool: verb): {misplaced}"
    )


_check_table_invariants(SERVER_ASSIGNMENT, MAINT_ALIAS_MAPPING, PREFIX_SURFACE_DIVERGENCES)
