"""shared constants for dry-run mode across mutation services.

Centralized so that ``LifecycleService`` (would-be ``supersedes`` edge),
``GraphOpsService`` (would-be ``_create_edge_strict`` edge), the MCP-tool wrappers,
and the dry-run test suite all agree on the same value.
"""

# Sentinel ``id`` populated on edges returned from a dry-run mutation
# that would have created the edge on a real run. We use the **nil UUID**
# (RFC 9562 §5.9: ``00000000-0000-0000-0000-000000000000``) because the
# ``EdgeIdStr`` schema validator requires a UUID — a literal like
# ``"<dry-run>"`` would fail Pydantic validation. The nil UUID is the
# documented "absent identifier" convention; storage will never mint
# this value (uuid4 cannot produce it), so a caller that mistakes it
# for a real id and uses it in a follow-up call will fail loudly on
# the lookup. The response wrapper's ``dry_run=True`` echo is the
# primary signal; this constant is the per-edge belt-and-braces marker.
DRY_RUN_SENTINEL_EDGE_ID = "00000000-0000-0000-0000-000000000000"
