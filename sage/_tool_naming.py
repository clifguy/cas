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
  (currently ``bulk_`` only). Anchored to CAS-ADR-029. Consumed by the
  conformance gates only.
- ``extract_verb`` — helper returning a tool name's canonical verb
  segment after stripping any compound prefix.
- ``SERVER_ASSIGNMENT`` — tool name → MCP server name (``sage`` or
  ``sage_maint``). The registration authority for the two-server split
  per CAS-ADR-034: ``build_partitioned_server`` reads it to decide which
  tools each surface carries, and a registered tool with no row here
  fails registration rather than landing on a default surface.
- ``MCP_HTTP_MOUNTS`` — the Streamable HTTP mount paths and the surface
  each serves; the single source of truth for the app's mounter, the
  access-log filter, and the dispatch-time refusal that names where a
  tool lives (``SURFACE_MOUNT_PATHS`` is its per-surface view).
- ``TOOL_ALIASES`` — retired tool name → canonical name, covering every
  spelling a maintenance tool has carried (the ``admin_*`` and ``maint_*``
  generations). The alias middleware consults it on every ``call_tool``
  invocation so callers holding an older name keep working.
"""

from __future__ import annotations

from typing import Final

# ----------------------------------------------------------------------
# Verb taxonomy (durable; anchored to CAS-ADR-033)
# ----------------------------------------------------------------------

#: The general verb categories (CAS-ADR-033): every category a tool on
#: either surface may carry. Together with ``MAINT_ONLY_VERBS`` below they
#: form ``CANONICAL_VERBS``, the set the first segment of a tool name --
#: up to the first underscore, or after consuming a compound prefix --
#: must belong to; the verb-compliance gate test enforces this.
_GENERAL_VERBS: Final[frozenset[str]] = frozenset(
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

#: The whole verb taxonomy: the general categories plus the maintenance-only
#: outliers, composed so each verb is stated exactly once and a verb cannot
#: be canonical without belonging to one of the two classes.
CANONICAL_VERBS: Final[frozenset[str]] = _GENERAL_VERBS | MAINT_ONLY_VERBS


#: Prefixes that compose with the verb taxonomy. After stripping a
#: compound prefix from the beginning of a tool name, the remainder
#: must begin with a canonical verb. ``bulk`` is retained for the
#: ``bulk_ingest_document`` tool whose rename to ``ingest_documents``
#: is deferred to a follow-on revision. The maintenance surface's retired
#: prefixes (``admin``, then ``maint``; CAS-ADR-029) survive only as keys
#: of the dispatch-level alias table below, never in a registered name,
#: so they compose with nothing: a name that reintroduced one would fail
#: the verb gate, since neither token is a verb.
COMPOUND_PREFIXES: Final[frozenset[str]] = frozenset({"bulk"})


def extract_verb(tool_name: str) -> str:
    """Return the canonical verb segment of a tool name.

    Strips any leading compound prefix (e.g. ``bulk_``), then returns
    the first underscore-separated segment of the remainder. Returns the
    empty string if ``tool_name`` is empty.

    Examples:
        ``extract_verb("get_document")`` returns ``"get"``.
        ``extract_verb("bulk_ingest_document")`` returns ``"ingest"``.
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
#: Nothing about a tool's name decides or signals its surface: the
#: maintenance prefix CAS-ADR-029 once carried is retired, so a name
#: describes the operation and this row alone describes the placement.
#: The partition gates hold this table to a hand-maintained pin.
SERVER_ASSIGNMENT: Final[dict[str, str]] = {
    # sage server (ordinary surface)
    # Vault enumeration: the ordinary surface is vault-addressed, so it
    # carries the one tool that lists the values its ``vault_id``
    # arguments range over.
    "list_vaults": "sage",
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
    "get_vault_config": "sage_maint",
    "get_vault_stats": "sage_maint",
    "get_stack_config": "sage_maint",
    "create_vault": "sage_maint",
    "reload_vault": "sage_maint",
    "update_vault_config": "sage_maint",
    "verify_vault_drift": "sage_maint",
    "verify_vault_source_files": "sage_maint",
    "restore_vault_source_file": "sage_maint",
    "migrate_vault": "sage_maint",
    "recompute_views": "sage_maint",
    "recompute_deferred_vault_abstracts": "sage_maint",
    "optimize_vault_content_store": "sage_maint",
}


#: Canonical HTTP MCP mount points as ``(mount_path, surface)`` pairs
#: (CAS-ADR-034). Single source of truth for the mounter in ``sage.app``,
#: the ``uvicorn.access`` suppression filter in ``sage.__main__``, and the
#: dispatch-time refusal that names where a tool lives: a mount added here
#: is covered by all three without a second edit. Held in this leaf module
#: rather than in the app module so the dispatch layer can read it without
#: importing the app that mounts it.
MCP_HTTP_MOUNTS: Final[tuple[tuple[str, str], ...]] = (
    ("/mcp", "sage"),
    ("/mcp_maint", "sage_maint"),
    # Pre-rename alias path for the maintenance surface (CAS-ADR-034):
    # identical roster, kept working with no scheduled removal.
    ("/mcp_admin", "sage_maint"),
)


def _paths_by_surface(mounts: tuple[tuple[str, str], ...]) -> dict[str, tuple[str, ...]]:
    """Group mount paths by the surface they serve, in mount-table order."""
    grouped: dict[str, list[str]] = {}
    for path, surface in mounts:
        grouped.setdefault(surface, []).append(path)
    return {surface: tuple(paths) for surface, paths in grouped.items()}


#: Server name → every mount path that serves it, canonical path first.
#: Derived from ``MCP_HTTP_MOUNTS``; consumed by the dispatch layer when it
#: refuses a tool that registers on the other surface, and by the table
#: invariants as the set of surfaces a tool may be assigned to.
SURFACE_MOUNT_PATHS: Final[dict[str, tuple[str, ...]]] = _paths_by_surface(MCP_HTTP_MOUNTS)


# ----------------------------------------------------------------------
# Retired-name aliases (anchored to CAS-ADR-034)
# ----------------------------------------------------------------------

#: Retired tool name → canonical name. The maintenance tools have carried
#: two prefixes since their names were last stable: ``admin_`` (renamed
#: away because it implied a privilege boundary the surface does not
#: carry -- CAS-ADR-034: uniform auth across both surfaces; the split
#: exists only to keep the ordinary catalog small) and then ``maint_``
#: (retired because, once the surface-assignment table above became the
#: registration authority, the prefix described nothing the table did
#: not, and misdescribed the placement of the one tool the table had
#: moved -- CAS-ADR-029). Every spelling from either generation resolves
#: here. The alias middleware in ``sage.mcp_server._LoggingFastMCP.call_tool``
#: consults this table on every invocation and rewrites a retired name
#: onto its canonical target, so callers holding either older name keep
#: working indefinitely -- no removal is scheduled. The retired names are
#: deliberately **not** registered as tools: the advertised catalog carries
#: only the canonical names.
#:
#: What the alias reaches, and what it costs. It protects a caller that
#: sends a retired name on the wire -- a raw JSON-RPC client, a script, a
#: configuration that names tools by string. It cannot reach a
#: catalog-validating client, one that resolves a tool name against
#: ``tools/list`` before any request leaves the process: such a client sees
#: only the advertised catalog, so a retired name in its configuration must
#: be updated, not aliased. And an aliased call is correct but not free:
#: the SDK looks the *requested* name up in its tool-definition cache
#: before this layer rewrites it, misses, rebuilds the catalog cache, and
#: draws an SDK WARNING alongside the one logged here. The alias is a
#: compatibility path, not a steady state.
#:
#: One flat table rather than one per generation: a caller need not know
#: which generation it holds, and dispatch stays a single lookup
#: (CAS-ADR-034 prices the alias at one dispatch-time lookup). The
#: import-time invariants make the flatness safe by refusing an alias
#: whose target is itself an alias, so a one-hop lookup is provably
#: complete.
#:
#: The table is written out rather than derived from ``SERVER_ASSIGNMENT``
#: on purpose: it covers exactly the spellings that existed. A maintenance
#: tool added after a rename gets no fabricated alias for the prefix it
#: never carried (``restore_vault_source_file`` post-dates the ``admin_``
#: era and has no ``admin_`` row), and a tool that has since moved to the
#: ordinary surface keeps the aliases it already had -- an alias follows
#: the name, and resolves on whichever surface registers the target
#: (CAS-ADR-034: an alias grants nothing the canonical name does not).
TOOL_ALIASES: Final[dict[str, str]] = {
    # admin_ generation
    "admin_list_vaults": "list_vaults",
    "admin_get_vault_config": "get_vault_config",
    "admin_get_vault_stats": "get_vault_stats",
    "admin_get_stack_config": "get_stack_config",
    "admin_create_vault": "create_vault",
    "admin_reload_vault": "reload_vault",
    "admin_update_vault_config": "update_vault_config",
    "admin_verify_vault_drift": "verify_vault_drift",
    "admin_verify_vault_source_files": "verify_vault_source_files",
    "admin_migrate_vault": "migrate_vault",
    "admin_recompute_views": "recompute_views",
    "admin_recompute_deferred_vault_abstracts": "recompute_deferred_vault_abstracts",
    "admin_optimize_vault_content_store": "optimize_vault_content_store",
    # maint_ generation
    "maint_list_vaults": "list_vaults",
    "maint_get_vault_config": "get_vault_config",
    "maint_get_vault_stats": "get_vault_stats",
    "maint_get_stack_config": "get_stack_config",
    "maint_create_vault": "create_vault",
    "maint_reload_vault": "reload_vault",
    "maint_update_vault_config": "update_vault_config",
    "maint_verify_vault_drift": "verify_vault_drift",
    "maint_verify_vault_source_files": "verify_vault_source_files",
    "maint_restore_vault_source_file": "restore_vault_source_file",
    "maint_migrate_vault": "migrate_vault",
    "maint_recompute_views": "recompute_views",
    "maint_recompute_deferred_vault_abstracts": "recompute_deferred_vault_abstracts",
    "maint_optimize_vault_content_store": "optimize_vault_content_store",
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
) -> None:
    """Assert the cross-table invariants; called once at import.

    Factored so a test can exercise the checks against a deliberately
    broken copy without re-importing the module.
    """
    # SERVER_ASSIGNMENT values must name a surface some mount serves.
    unknown_servers = set(assignment.values()) - set(SURFACE_MOUNT_PATHS)
    assert unknown_servers == set(), (  # noqa: S101
        f"SERVER_ASSIGNMENT values must be one of {sorted(SURFACE_MOUNT_PATHS)}: {unknown_servers}"
    )

    # Every alias must target a registered name (on either surface -- the
    # alias follows the name, not the surface), no alias may collide with
    # a registered name (a collision would shadow a live tool behind the
    # rewrite), and no alias may target another alias (the dispatch layer
    # resolves exactly one hop, so a chained entry would resolve to a name
    # that is not registered).
    chained_aliases = {old for old, new in aliases.items() if new in aliases}
    assert chained_aliases == set(), (  # noqa: S101
        f"alias(es) whose target is itself an alias (one-hop resolution): {chained_aliases}"
    )
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
    misplaced = {
        name: extract_verb(name)
        for name, server in assignment.items()
        if extract_verb(name) in MAINT_ONLY_VERBS and server != "sage_maint"
    }
    assert misplaced == {}, (  # noqa: S101
        "tool(s) carrying a maintenance-only verb registered off the "
        f"maintenance surface (tool: verb): {misplaced}"
    )


_check_table_invariants(SERVER_ASSIGNMENT, TOOL_ALIASES)
