# MCP Profile-Invariance Tests (MPI)

Behavioral tests proving the MCP document import/export byte channel is
binding-invariant: an identical MCP call yields an identical result whether
the vault-source store is bound to the local filesystem or to the document
store. The caller supplies local paths and never learns which binding
retains the bytes.

Every test in `test_mcp_profile_invariance.py` runs twice via the
`vault_source_backend` fixture (`tests/sage/conftest.py`): once per binding.
The document-store leg runs the real `DocumentStoreVaultSourceStore` over an
in-memory `FakeGraphClient` (`tests/helpers/fake_graph_client.py`); only the
Graph transport is faked, and its per-operation counters are the
anti-coincidental teeth — several scenarios were already green when authored
(the port routing pre-dated this suite), and the counters are what make that
green meaningful.

Design decisions encoded here:
- These tests exercise the co-located topology: the server can read and
  write the caller's paths directly, and the vault-source binding
  (CAS-ADR-043) decides where the bytes are retained -- that is the
  invariance axis under test. The remote topology, where the server cannot
  see the caller's filesystem and the same calls return token-gated
  transfer recipes whose byte legs the caller's environment runs, is
  covered by `test_mcp_caller_fs_confinement.py` and
  `test_transfer_endpoints.py`; the recipe plumbing lives below the tool
  contract, so the call shape stays identical across deployments
  (CAS-ADR-042 constraint 1).
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

## MPI-008: app lifespan discovers and registers a document-store-backed vault with no local vault tree

**Artifact:** `sage/app.py` (lifespan)
**Category:** lifespan, profile_invariance

Lives in `tests/sage/test_vault_discovery.py` (that module's fixtures own
lifespan faking); listed here for the invariance suite's completeness.

**Precondition:** `SAGE_TEST_VAULT_SOURCE_BACKEND=document_store`; a fake Graph
client holding one vault config; `_initialize_vault` faked.

**Input:** run the app lifespan.

**Expected:** the vault registers; the discovered vault carries
`config_path=None`; no local vault directory is required or created.

**Anti-coincidental-pass:** discovery regressed to a filesystem-rooted config
walk would not find the fake-backed vault at all.

## MPI-009: the recorded provenance hash is the hash of the delivered bytes

**Artifact:** `sage/services/ingestion.py` (delivered-hash capture),
`sage/vault_source_binding.py` (`hash_file`)
**Category:** mcp_tool, profile_invariance, import, provenance

**Precondition:** One registered vault; a fresh `.docx` at a caller-local
absolute `tmp_path` (outside `storage_root`). An Office package, not markdown,
because that is the format a document store rewrites at rest — the divergence
under test does not exist for a format the store keeps verbatim.

**Input:** `ingest_document(vault_id, source=<absolute path>, source_type="docx")`.

**Expected:**
- `source_content_hash == sha256(<the caller's file bytes>)` on **both** legs.
- Filesystem leg: `stored_content_hash == source_content_hash` (this binding
  keeps what it was given; the regression guard that its behavior did not move).
- Document-store leg: `stored_content_hash != source_content_hash`, and it
  equals the digest of the bytes the fake actually holds.

**Anti-coincidental-pass:** the document-store leg asserts
`fake.stamped_uploads >= 1` — the store must have genuinely rewritten the
upload. Without that, the headline equality is satisfied by a store that
happened to retain the bytes verbatim, which is exactly the condition under
which this defect stayed invisible: the shared fake stored every upload
unchanged, so no existing test could observe it. The rewrite in
`tests/helpers/fake_graph_client.py` (a fresh `customXml/itemProps<N>.xml` part
per upload, mirroring the item GUIDs the real service mints) is what gives every
test in this group teeth.

## MPI-010: byte-identical re-ingest dedups and leaves no second stored copy

**Artifact:** `sage/services/ingestion.py` (`_retain_or_reuse`, duplicate
detection), `sage/vault_source_binding.py` (`planned_source_path`)
**Category:** mcp_tool, profile_invariance, import, dedup

**Precondition:** MPI-009's state — one `.docx` already ingested from a
caller-local absolute path.

**Input:** `ingest_document` again, same absolute path, same name.

**Expected:**
- `duplicate_content` envelope naming the first document's id.
- Exactly one document in the vault.
- Document-store leg: `fake.source_uploads` unchanged across the second call,
  and no new key in `fake.sources`.

**Anti-coincidental-pass:** the duplicate envelope alone does not prove the
second defect is fixed. Retention runs *before* duplicate detection, so a
binding that re-uploaded would still return the right error while leaving a
disambiguated `imports/<stem>_<hash8>.<ext>` copy behind. The upload counter is
the assertion with teeth; the error code is not.

## MPI-011: identical bytes under a different filename still dedup

**Artifact:** `sage/services/ingestion.py` (duplicate detection)
**Category:** mcp_tool, profile_invariance, import, dedup

**Precondition:** MPI-009's state; a byte-identical copy of the `.docx` at a
second caller-local name.

**Input:** `ingest_document(vault_id, source=<the renamed copy>, source_type="docx")`.

**Expected:** `duplicate_content` on both legs — detection is hash-keyed, not
name-keyed — with exactly one document in the vault.

**Anti-coincidental-pass:** the envelope's `source_content_hash` is asserted to
equal the *delivered* digest. Detection keyed to the as-stored digest could not
match here at all, since the store mints a fresh copy per upload; pinning the
reported value is what identifies which digest the rejection was keyed to.

## MPI-012: an unforced re-delivery leaves an out-of-band-altered copy untouched

**Artifact:** `sage/services/ingestion.py` (`_retain_or_reuse`)
**Category:** mcp_tool, profile_invariance, import, integrity

**Precondition:** MPI-009's state; the retained copy then altered by something
other than SAGE.

**Input:** `ingest_document` again with the original bytes, unforced.

**Expected:** `duplicate_content`; no write to the store; the source-file
integrity audit still reports one `hash_mismatch`.

**Anti-coincidental-pass:** the alteration is asserted to have taken effect, so
this is the drifted branch and not the ordinary identical-copy path — and the
audit is actually *run*, not asserted about in prose. An implementation that
overwrote the drifted copy would leave the audit clean and fail the final
assertion, which is the whole claim: a silent repair also erases the operator's
only evidence that something else wrote to the store.

## MPI-013: a forced re-delivery does not launder a drifted copy

**Artifact:** `sage/services/ingestion.py` (the force branch's conditional
`stored_content_hash` refresh)
**Category:** mcp_tool, profile_invariance, import, integrity

**Precondition:** as MPI-012.

**Input:** `ingest_document` again with the original bytes, `force=true`.

**Expected:** success, same `source_path`, and the audit *still* reports one
`hash_mismatch`; the reported `expected_content_hash` equals the document's
recorded `stored_content_hash`.

**Anti-coincidental-pass:** the audit is asserted **red**, which is the claim.
The rival this excludes — refreshing the as-stored digest unconditionally on the
force branch — leaves the audit green while changing nothing else observable,
so without this assertion it is invisible. Note what this test does *not* claim:
a forced re-ingest does not repair the drift, and is not meant to. Repair is a
separate operation the operator invokes on purpose (MPI-017); keeping it off the
ingest path is deliberate, since an ingest that silently restored would erase
the operator's evidence that something else wrote to the store. A forced
re-ingest could not stand in for it in any case — retention sees only that the
bytes at its target differ from the ones offered, which is indistinguishable
from a name collision, so it disambiguates rather than overwrites.

## MPI-014: re-delivering a collision-disambiguated document leaves its copy intact

**Artifact:** `sage/services/ingestion.py` (`_retain_or_reuse`, keyed on the
delivered digest), `sage/vault_source_binding.py` (collision disambiguation)
**Category:** mcp_tool, profile_invariance, import, dedup

**Precondition:** two documents sharing a basename on a restamping store, the
second disambiguated to `imports/<stem>_<hash8>.<ext>`.

**Input:** `ingest_document` with the *second* document's exact bytes.

**Expected:** `duplicate_content`; the second document's stored copy is
byte-unchanged; no upload; the audit reports zero mismatches.

**Anti-coincidental-pass:** the collision is constructed and asserted (the two
documents really do land on different paths), and the stored bytes are compared
directly. Asserting only the duplicate envelope passes against the defect —
the rejection happens either way. This is the path a *name*-keyed reuse check
cannot see: it reads the un-disambiguated destination, finds the other
document's hash, falls through to a retain, and — because the disambiguating
suffix derives from the delivered bytes — rewrites this document's copy with a
freshly stamped one while duplicate detection then declines the ingest, leaving
the record describing a copy that no longer exists.

## MPI-015: a resident-source re-ingest dedups rather than duplicating

**Artifact:** `sage/services/ingestion.py` (resident-path provenance inheritance)
**Category:** mcp_tool, profile_invariance, import, dedup

**Precondition:** MPI-009's state; the bytes live on the store.

**Input:** `ingest_document` with the document's *vault-relative* path — the
post-restart cloud condition that branch exists for.

**Expected:** `duplicate_content`, and exactly one document in the vault.

**Anti-coincidental-pass:** asserts the document count, not just the envelope.
Nothing is delivered on this call, so provenance is inherited from the record
that established the path; deriving it from the stored copy instead gives the
re-projection a different identity from the document it is re-projecting, and
with no unique index on `source_path` a missed duplicate is an *insert*, not an
error — so the error code alone cannot discriminate, because the defective path
raises nothing at all.

## MPI-016: reuse falls through when the stored copy is gone

**Artifact:** `sage/services/ingestion.py` (`_retain_or_reuse`'s presence check)
**Category:** mcp_tool, profile_invariance, import

**Precondition:** MPI-009's state; the retained copy then deleted from the store.

**Input:** `ingest_document` again with the original bytes, `force=true`.

**Expected:** success, and the returned `source_path` holds bytes on the store.

**Anti-coincidental-pass:** the deletion is asserted, so the presence check is
the only thing between this call and a reuse onto a path holding nothing.
Mutating the guard away leaves no copy at the returned path and fails the final
presence assertion. This is the third fall-through condition in
`_retain_or_reuse`; the other two (no document with these bytes, and a stored
copy whose digest differs) are covered by MPI-009 and MPI-014.

## MPI-017: an explicit restore repairs a drifted copy under either binding

**Artifact:** `sage/vault_source_binding.py` (`write_source` on both bindings),
`sage/services/maintenance.py` (`restore_vault_source_file`)
**Category:** mcp_tool, profile_invariance, integrity, repair

**Precondition:** as MPI-012 — a document ingested, its retained copy then
altered out of band, and the audit asserted red.

**Input:** `restore_vault_source_file` with the original source file.

**Expected:** `status: restored`, the same `source_path` as the record already
held, and the audit clean afterwards. Provenance is unchanged on both legs. On
the document-store leg the upload counter has moved and the record's
`stored_content_hash` equals a fresh re-read of the stored copy; on the
filesystem leg the bytes at the retained path equal the delivered file's.

**Anti-coincidental-pass:** the drift is asserted real (audit red) before the
restore, so the green verdict afterwards cannot come from an unaltered copy. The
rival that leaves the audit equally green while repairing nothing — adopting the
drifted digest as the expected state — is excluded by asserting the *bytes*
rather than the verdict: the retained copy is re-read and required to hash to
what the record now expects. The upload-counter assertion on the document-store
leg keeps "restored" from being satisfied by a binding that stored nothing, and
the `source_path` assertion distinguishes a write-in-place from the
collision-disambiguated second copy `retain_source` would have produced.
