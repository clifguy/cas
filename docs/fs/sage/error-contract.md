# Error contracts

REST error envelopes use `code` to select one mutually exclusive variant of
`ErrorResponse`. Each known variant declares its required context, optional
context and value types. A no-detail error omits `detail`. Required nullable
values stay present, such as an absent pinned content hash; conditional context
is omitted when unavailable. Malformed-input echoes deliberately admit arbitrary
JSON where the refused input can have any shape.

An optional key is one a constructor sets when it is given the argument that
carries it; a family declares no optional key its constructors cannot set. The
contract tests hold both directions: every emitted key is declared, and every
declared optional key is emitted by some constructor supplied its optional
arguments.

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
The authoritative `ErrorResponse.x-mcp-tool-errors` table enumerates each
registered tool's boundary, service and delegated/callback families. Prose does
not select families: a discussion of a different tool's error is not a declaration.
Both surfaces publish explicit discriminator mappings for their known codes.
Unknown REST extensions use the extension union arm without a mapped discriminator
entry; JSON Schema union validation remains available for those downstream codes. Authentication and byte-transfer HTTP endpoints
retain their own transport contracts.

The catalog is regenerated with `python -m scripts.dump_mcp_catalog --write`.
These schema declarations preserve existing payloads, statuses and read markers.
