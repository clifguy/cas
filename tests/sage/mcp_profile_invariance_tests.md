# MCP Profile-Invariance Tests (MPI)

Behavioral tests proving the MCP document import/export byte channel is
profile-invariant: an identical MCP call yields an identical result whether the
vault's stores are bound locally (filesystem vault-source binding) or
cloud-hosted (document-store binding). The caller supplies local paths, never
learns the vault's location, and never selects a transport.

Every test in `test_mcp_profile_invariance.py` runs twice via the
`vault_source_backend` fixture (`tests/sage/conftest.py`): once per binding.
The document-store leg runs the real `DocumentStoreVaultSourceStore` over an
in-memory `FakeGraphClient` (`tests/helpers/fake_graph_client.py`); only the
Graph transport is faked, and its per-operation counters are the
anti-coincidental teeth — several scenarios were already green when authored
(the port routing pre-dated this suite), and the counters are what make that
green meaningful.

Design decisions encoded here:
- The supported topology for path-bearing tools is a server co-located with
  the caller; a cloud-hosted vault is reached by binding the storage ports
  remotely (CAS-ADR-042 invariance, CAS-ADR-043 swappable vault-source
  binding). No caller-side transport selection exists.
- Result-shape equality across legs means: identical top-level response keys,
  identical values for location-independent fields (`source_path`, counts,
  status strings, `content_hash`), and shape-only comparison for volatile
  fields (document ids, timestamps, absolute tmp paths).
- Import sources live only in caller-local temporary directories, never
  pre-seeded under the vault's `storage_root`, so retention is forced through
  the active binding.
- Out-of-band divergences accepted by design: (1) `retain_source`'s upload
  buffers the whole file (a chunked Graph upload session is a separate
  adapter concern); (2) a *relative* ingest `source` that exists on the local
  tree is retained in place under the filesystem binding but re-homed to
  `imports/<name>` under the document-store binding — the MCP import contract
  is absolute caller paths, where both bindings agree.

---

## MPI-001: ingest_document imports a caller-local file under either binding

**Artifact:** `sage/services/ingestion.py` (source resolution), `sage/vault_source_binding.py`
**Category:** mcp_tool, profile_invariance, import

**Precondition:** One registered vault; a fresh markdown file at a caller-local
absolute `tmp_path` (outside `storage_root`).

**Input:** `ingest_document(vault_id, source=<absolute path>, source_type="markdown")`.

**Expected:**
- Success envelope with `id`; `source_path == "imports/<name>"` on both legs.
- Retained bytes on the active backend are byte-identical to the original
  (filesystem: `storage_root/imports/<name>`; document store: the fake's
  `sources["imports/<name>"]`).
- Document-store leg: `fake.source_uploads == 1`.

**Anti-coincidental-pass:** an implementation that copies into `storage_root`
with raw filesystem I/O instead of `retain_source` leaves the fake's `sources`
empty and `source_uploads == 0` — the document-store leg fails even though the
filesystem leg (where server-local and caller-local coincide) still passes.

## MPI-002: bulk_ingest_document imports caller-local files under either binding

**Artifact:** `sage/services/batch_ingest.py`, `sage/app_tools.py`
**Category:** mcp_tool, profile_invariance, import

**Precondition:** One registered vault; two fresh markdown files in a
caller-local `tmp_path` directory.

**Input:** `bulk_ingest_document(vault_id, files=[{file_path, source_type}, ...])`.

**Expected:**
- `documents_created.new == 2`, `error_count == 0` on both legs.
- Both files' bytes retained on the active backend.
- Top-level summary keys identical across legs.

**Anti-coincidental-pass:** a raw `Path`-existence or local-mirror gate inside
the batch service would land per-file errors in `summary.errors[]` on the
document-store leg (no local mirror exists), breaking `error_count == 0`.

## MPI-003: list_directory discovery is caller-local and vault-checked through the graph store

**Artifact:** `sage/app_tools.py` (`list_directory`), `sage/services/scan.py`
**Category:** mcp_tool, profile_invariance, import_discovery

**Precondition:** One registered vault; a caller-local `tmp_path` directory
holding one `.md` file and one unknown-extension file.

**Input:** `list_directory(vault_id, directory=<abs dir>)`; then ingest the
`.md` via `ingest_document(source=<abs path>)` and scan again.

**Expected:**
- First scan: identical `files[]` shapes and `warnings` across legs; the `.md`
  reports `sage_status == "new"`, the other `no_adapter`.
- Second scan: the ingested file reports `sage_status == "unchanged"` on both
  legs.

**Anti-coincidental-pass:** the freshness check must go through the graph
store's hash lookup. A check that stats the vault's local tree would report
`new` on the document-store leg (no local retained copy exists there).

## MPI-004: get_document write_to_path exports to a caller-local path under either binding

**Artifact:** `sage/services/documents.py` (`_deliver_to_path`)
**Category:** mcp_tool, profile_invariance, export

**Precondition:** A document ingested from a caller-local absolute path
(MPI-001 shape).

**Input:** `get_document(vault_id, document_id, write_to_path=<abs tmp target>)`.

**Expected:**
- Target file written, byte-identical to the original source.
- `content_hash` equals `sha256:<hex>` of the original bytes; `written_to` and
  `content_size` populated; `content` null; `read_meta.body_present` false.

**Anti-coincidental-pass:** delivery that opens `storage_root / source_path`
directly finds no file on the document-store leg and errors; byte-identity
plus the recorded hash pin the bytes to the retained source, not an empty or
partial write.

## MPI-005: read_projection write_to_path spills caller-locally without consulting the vault-source store

**Artifact:** `sage/services/utilities.py` (`read_projection`)
**Category:** mcp_tool, profile_invariance, export

**Precondition:** A document ingested and projected (MPI-001 shape).

**Input:** `read_projection(vault_id, document_id, write_to_path=<abs tmp target>)`.

**Expected:**
- Projection text written to the caller-local path; non-empty; contains the
  source body text.
- `written_to`/`content_size` populated; `projection_text` null; response
  metadata shape identical across legs.
- Document-store leg: the fake's `source_reads`/`source_streams`/`source_stats`
  counters do not advance during the read (projection text comes from the
  content store, not the vault-source store).

**Anti-coincidental-pass:** the zero-counter assertion proves the projection
spill channel genuinely never touches the vault-source port; a reimplementation
that re-projects from source bytes would advance the counters and fail.

## MPI-006: the export byte channel streams and is not bounded by the inline ceiling

**Artifact:** `sage/services/documents.py` (`_deliver_to_path`, `_attach_inline_content`)
**Category:** mcp_tool, profile_invariance, export, large_file

**Precondition:** `SAGE_MAX_INLINE_CONTENT_BYTES=1024`; a ~64 KiB file ingested
from a caller-local absolute path.

**Input:** `get_document(include_content=true)`, then
`get_document(write_to_path=<abs tmp target>)`.

**Expected:**
- Inline read: `content_too_large` error envelope on both legs (the ceiling
  governs inline delivery only).
- Write-to-path: full byte-identical round trip on both legs — the byte
  channel is not bounded by the inline ceiling.
- Document-store leg: `fake.source_streams` records the delivery and
  `fake.source_reads` does **not** advance — the delivery went through the
  streaming read, not the buffered whole-body read.

**Anti-coincidental-pass:** the counter pair is the streaming proof: a
buffered `read_source` delivery round-trips the same bytes but fails the
`source_reads` assertion. The tiny ceiling with a 64x payload proves
`write_to_path` is the working escape hatch above the inline bound.

## MPI-007: path-bearing tool docstrings state the server-local path contract

**Artifact:** `sage/sage_api_tools.py`, `sage/app_tools.py`
**Category:** mcp_tool, self_documentation

**Precondition:** None (static docstring inspection).

**Input:** `inspect.getdoc` over `ingest_document`, `bulk_ingest_document`,
`get_document`, `read_projection`, `list_directory`.

**Expected:** each docstring states that its path parameter (`source`,
`files[].file_path`, `write_to_path`, `directory`) resolves on the machine
running the SAGE server process.

**Anti-coincidental-pass:** catches silent docstring drift that would erase
the caller-local path contract from the tool surface's self-documentation.

## MPI-008: stdio lifespan discovers and registers a document-store-backed vault with no local vault tree

**Artifact:** `sage/mcp_server.py` (`_lifespan`)
**Category:** lifespan, profile_invariance

Lives in `tests/sage/test_mcp_server_lifespan.py` (that module's fixtures own
lifespan faking); listed here for the invariance suite's completeness.

**Precondition:** `SAGE_TEST_VAULT_SOURCE_BACKEND=document_store`; a fake Graph
client holding one vault config; `initialize_services` faked.

**Input:** run the server lifespan.

**Expected:** the vault registers; the discovered vault carries
`config_path=None`; no local vault directory is required or created.

**Anti-coincidental-pass:** discovery regressed to a filesystem-rooted config
walk would not find the fake-backed vault at all.
