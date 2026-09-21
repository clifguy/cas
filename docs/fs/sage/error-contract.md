# Error contracts

REST error envelopes use `code` to select one mutually exclusive variant of
`ErrorResponse`. Each known variant declares its required context, optional
context and value types. A no-detail error omits `detail`. Required nullable
values stay present, such as an absent pinned content hash; conditional context
is omitted when unavailable. Malformed-input echoes deliberately admit arbitrary
JSON where the refused input can have any shape.

Both REST specifications publish the same families. The Core specification is the
authority for shared definitions; `scripts/generate_error_contract.py` derives the
packaged runtime projection. Strict Pydantic variants and the live REST OpenAPI
schema derive from that projection. Generated files and the Application mirror
are checked against the source. An extension envelope permits downstream codes,
but explicitly excludes every known code, so it cannot rescue invalid context
for a known family.

Every MCP tool publishes `_meta["org.sage/errorSchema"]` through `tools/list`.
It is a JSON Schema with local `$defs` references and the existing `error`
discriminator. It describes the error JSON carried inside text content, not
structured successful output; `outputSchema` continues to describe successes.
Each tool carries its documented error families and common argument/routing
refusals. An envelope's `x-mcp-tools` supplement records a reachable family not
spelled out in that tool's prose. Authentication and byte-transfer HTTP endpoints
retain their own transport contracts.

The catalog is regenerated with `python -m scripts.dump_mcp_catalog --write`.
These schema declarations preserve existing payloads, statuses and read markers.
