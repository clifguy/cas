# Operational gap and portability map

Baseline: retained bytes and current linear heads in `source-provenance.json`,
read September 27, 2026 against served build `3.0.2+a0f8b13`. Source descriptions
and the target's live configuration remain authoritative; examples below do not
freeze parameter lists. Source inspection is supporting evidence, not proof that
an unreleased checkout behavior is deployed.

| Gap | Evidence and disposition | Destination | Acceptance scenario |
|---|---|---|---|
| 1 re-anchoring | Hosted writing baseline explicitly forbids duplicate inherited edges; older sage-operations contradicts it. Preserve correct rule; compatibility copy waits for cutover. | writing, retrieval, diagnosis | Successor has no rows, predecessor valid reference: no graph write. |
| 2 literal anchors | Served traverse describes chain resolution and outbound dedup; search edge target supplies literal filters. | retrieval | Predecessor traversal surfaces indirect dependent; inspect exact edge rows before concluding ownership. |
| 3 repeated identity | Served chain reports total length, linearity and head, independent of page. | retrieval | Two divergent titles in one chain are revisions; disconnected third candidate is a collision. |
| 4 call shapes | Served batch/create_edges/ingest schemas enumerate items, ID anchors, required anchors, rationale enum and source. Avoid duplicating changing signatures. | writing / Creating graph relationships | Read supplied current schema and construct conformant call; reject label-shaped anchor. |
| 5 metadata | Served ingest rejects misplaced metadata; catalog-validating clients can strip unsupported parameters first. | writing, diagnosis | Fresh ID absent from filtered catalog: read row and compare intended metadata before retry. |
| 6 projection race | Served projection explicitly lists no_projection and pipeline diagnosis; recomputation is mutating. | diagnosis | Pending projection: bounded reads. Authorized repair returning already_in_flight: stop repairing and observe. |
| 7 section replacement | Served update_vault_config states wholesale replacement; service compares submitted values to current default-filled section. | configuration | Omitted defaults: rebuild from full read, inspect preview and verify after write. |
| 8 completed revision | Hosted writing establishes lifecycle scope; live vault transition tables control completed supersession. | writing | Successful new active head restores prior completed state only within existing scope; failed pipeline stays incomplete. |
| 9 schema narrowing | Tier-3 filter-key validation and write-schema validation are distinct; config replaces document_types wholesale. | configuration | Widen enum, authorized backfill, verify rows, narrow; old-value read is not write validity. |
| 10 binary bytes | Served document delivery distinguishes source bytes; REST contract has binary_content_refused and raw content route. | retrieval, runtime, transfer | Real DOCX/PDF/PPTX/XLSX bytes preserve hash; Markdown success is not binary acceptance. |
| 11 source_types | Source filename parser applies source_types constraints; explicit doc_type resolves separately. | configuration | Explicit supported format/type needs no unsolicited config edit. |
| 12 bulk enumeration | Served search defines paged catalog/light response; API exposes same operation. | retrieval | Enumerate pages once; union separate tag queries for OR; fetch detail for omitted tags. |
| 13 scoped verification | Concurrent writes invalidate vault totals as no-drift control. | retrieval | Reread touched IDs and lineage while unrelated writer changes facets; do not assert vault immutability. |

Reference names above are under `skills/sage/reference/`. Tests execute actual
package and installer code. Prose acceptance must inspect observed agent decisions;
string retention is not behavioral acceptance. Live mutating scenarios stay pending
until separately initiated smoke-test authorization.

| Portability obligation | Reuse/adaptation | Destination and acceptance |
|---|---|---|
| 1 shared core/actual host | Reuse hosted core/runtime, extend task references; no host prose forks. | runtime.md; Codex and Claude select their actual tools, not directory-label tools. |
| 2 full dependency graph | Adapt CAS verifier for all Markdown relative dependencies and complete inventory. | sage_package.py; missing/source-only path fails; detached package works after checkout removal. |
| 3 personal defaults | Adapt shared standalone layout; explicit roots, no project workflow prerequisite. | standalone_layout.py; personal Codex and Claude native trials select exact installed path. |
| 4 reviewed engine reuse | Extract upstream generic transaction engine, consume exact export and pin. | scripts/sage_installer/runtime-manifest.json; shared regression suite and CAS integration trials; no second transaction implementation. |
| 5 transaction controls | Reuse collision adoption, stale-plan guard, locks, ownership, journal, backup and rollback. | transaction_engine.py; deterministic fault/guard tests and untouched unrelated files. |
| 6 metadata/integrity | Adapt personal target mapping; metadata outside skills, bytecode disabled before imports. | installer.py; host skills/manifest.json survives and post-execution full package verify passes. |
| 7 duplicates/selection | Inventory competing copies; native invocation distinct from catalog and guide load. | migration.md + retained host trial evidence; two project contexts share one temporary user installation. |
| 8 transfer safeguards | Reuse retained transfer.md unchanged, with actual-host runtime selection. | transfer.md; adversarial origin/hash/collision/uncertain-completion decisions, then separately authorized live smoke. |

No obligation is declared non-applicable. Windows/native non-POSIX installation is
not claimed by the POSIX engine; execution capability limitations must be reported.
