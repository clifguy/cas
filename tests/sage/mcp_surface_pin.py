"""Hand-maintained oracle for the SAGE MCP surface partition.

``EXPECTED_SURFACE`` is the tool name → surface mapping the partition
gates hold the live system to. It is a literal transcription written by
hand, deliberately **not** derived from ``sage._tool_naming.SERVER_ASSIGNMENT``
(the table registration reads) and **not** imported by any production
code: its only value is its independence. A gate that compared the table
to a set derived from the table would pass for any table, including one
that quietly gained a tool or moved one between surfaces. Comparing the
table, the built servers, and the live HTTP mounts to this literal instead
means a tool added to a surface, dropped from one, or moved between them
without a deliberate edit here goes red in CI -- which has no vault and so
cannot consult the *SAGE MCP Tool Surface* steering document's registration
map, the human-facing enumeration this pin mirrors (CAS-ADR-029,
CAS-ADR-034).

The pin protects against omission and drift, not against a deliberate
global rewrite: a repo-wide substitution that edits the table and this file
in one motion passes, as it should -- that is a decision, and this file is
where the decision is recorded on the test side. Nor does it judge a new
row: an author who assigns a new tool to the wrong surface and transcribes
that row here satisfies the gate, since a name carries no placement signal
of its own. That is accepted -- the retired prefix was the same author's
judgment written twice -- and the check on a new row is the *SAGE MCP Tool
Surface* registration map and review, not this file. Keep the module free
of ``sage`` imports so the oracle cannot become an echo by accident.
"""

from __future__ import annotations

from typing import Final

#: Tool name → surface: ``sage`` (ordinary, the ``/mcp`` mount) or
#: ``sage_maint`` (maintenance, the ``/mcp_maint`` and ``/mcp_admin`` mounts).
EXPECTED_SURFACE: Final[dict[str, str]] = {
    # sage (ordinary surface)
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
    # sage_maint (maintenance surface)
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
