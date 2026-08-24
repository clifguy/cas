"""Shared MCP ``ToolAnnotations`` vocabulary for the SAGE tool surface.

Every tool registered on either Streamable HTTP mount declares an
explicit ``ToolAnnotations`` object, so a client reading ``tools/list``
can distinguish a pure read from a write from a destructive operation.
The two mounts and their partition are anchored to CAS-ADR-034; the
``maint_`` compound prefix that drives the partition is CAS-ADR-029.

These are hints in the protocol sense: honest self-description of the
surface, not an access-control mechanism. Nothing in SAGE grants or
withholds capability based on them.

Why each field is declared the way it is
----------------------------------------

Every ``ToolAnnotations`` field defaults to ``None`` and is dropped from
the serialized payload. The MCP specification then applies its own
client-side defaults to the omitted fields -- notably ``destructiveHint``
true and ``openWorldHint`` true. Declaring ``readOnlyHint`` alone would
therefore leave an additive writer such as ``create_edges`` reading as
destructive, and every vault-scoped tool reading as open-world. So:

``readOnlyHint``
    Declared on every tool. True only when no code path mutates vault
    state; writing a cached or derived artifact counts as a write.

``destructiveHint``
    Declared on every writer, and left unset on read-only tools, where
    the specification says it carries no meaning. False marks a writer
    whose updates are purely additive; True marks one that can overwrite
    or remove state a caller already has.

``openWorldHint``
    False throughout. SAGE operates on a closed vault domain. The one
    tool that reaches outside the vault reads a caller-local filesystem,
    which is still a closed, deterministic domain rather than the open
    external world the hint is meant to flag.

``idempotentHint``
    Deliberately unset on every tool. Its specification default (false)
    is already the conservative reading, so omitting it claims nothing
    that would later have to be defended per tool.

Classification is resolved against each tool's implementation rather
than its name: ``maint_recompute_views`` reads like a refresh but drops
its symlink trees with ``rmtree``, while ``get_document`` accepts a
``write_to_path`` yet mutates nothing in the vault.
"""

from __future__ import annotations

from typing import Final

from mcp.types import ToolAnnotations

#: No code path mutates vault state. ``destructiveHint`` is left unset:
#: the specification gives it no meaning when ``readOnlyHint`` is true.
READ_ONLY: Final[ToolAnnotations] = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=False,
)

#: Mutates state, but only by addition -- no overwrite, no removal.
WRITE_ADDITIVE: Final[ToolAnnotations] = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    openWorldHint=False,
)

#: Mutates state and may overwrite or remove what a caller already has,
#: on at least one reachable code path.
WRITE_DESTRUCTIVE: Final[ToolAnnotations] = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    openWorldHint=False,
)
