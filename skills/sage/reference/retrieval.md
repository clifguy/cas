# Retrieval, identity and graph evidence

Read the selected deployment's current descriptions for modes, filter shapes,
pagination and response fields. Use catalog enumeration for every matching record;
semantic search discovers relevance and cannot establish completeness. Use keyword
search for exact text, then read projections or exact stored heading paths.

## Enumerating and identifying current documents

Request light records for large sweeps, paginate through the declared total/cursor,
and fetch details only where needed. A light response can omit tags: missing fields
are not empty values. When the served tag filter combines values with AND, OR needs
one query per tag followed by a document-ID union. For large sweeps an available
authenticated REST client can execute one bounded scripted pagination pass; use its
live OpenAPI schema, configured endpoint and existing authentication. REST access is
not implied by MCP access. Avoid reconstructing full documents from search chunks.

A repeated tier-3 identifier can identify several revisions. Resolve every candidate
through the complete configured version chain, accounting for all pages and its
linearity. Divergent titles do not split a chain. Distinct disconnected chains under
one supposedly unique identifier are a collision to report, not an invitation to
pick the newest timestamp or repair identity. A fork, truncated chain or failed
lookup leaves currentness unresolved. Verify the chosen head's lifecycle as well.

## Effective edges versus stored anchors

Traversal applies chain resolution, anchors, retractions and merge tombstones. A
query from a predecessor can return a dependent whose literal edge never touches
that predecessor. Returned nodes do not establish literal edge ownership; outbound
deduplication can also mask sibling edges. Read the served traversal semantics and
compare edge counts when multiplicity matters.

To inspect actual anchors, enumerate production edge rows with an exact source or
target document filter. Use the chain operation for version history. An inherited
edge anchored on a valid predecessor is correct; **do not recreate or re-anchor it
merely because a successor exists**. Missing successor-owned rows do not mean lost
references. Verify validity and any retraction before proposing a scoped change.
Staged candidates and production edges are separate evidence.

## Projection versus retained source

A projection is the readable text surface, not an editing baseline or the original
binary file. Retrieve retained source bytes to edit and supersede. Use the served
source-delivery contract; inspect body form, size and hash, and execute any returned
recipe with [transfer guidance](transfer.md).

REST inline content can refuse binary formats with `binary_content_refused`; use
that deployment's raw content route (currently `/documents/{id}/content` under its
vault route) or its supported download recipe. Do not decode a projection as DOCX,
PDF, PPTX or XLSX. Verify with actual binary bytes and their original digest; success
on Markdown establishes nothing about binary retrieval. MCP and REST delivery
shapes may differ. Discover the route from the served contract, not this example.

Facet counts establish observed vocabulary, not schema or uniqueness. Free-form tags
can exceed result limits: narrow the query and label the vocabulary slice. Under
concurrent writers vault totals move; they cannot prove your operation changed
nothing else. Record and reread exactly the touched document identities, lifecycle,
metadata and lineage. A supersession can explain total +1/archived +1, but that
signature alone is not proof of correct identity or absence of unrelated changes.
