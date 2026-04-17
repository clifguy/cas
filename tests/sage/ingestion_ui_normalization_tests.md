# SAGE Ingestion UI-Layer Metadata Normalization Tests

Behavioral tests for sanitizing UI-layer invisibility markers when SAGE
imports an external file into a vault. These tests validate CAS-ADR-016:
"SAGE normalizes UI-layer file metadata on ingest."

Agents routinely flag their working temp files invisible for their own
hygiene. On macOS this is done via `chflags hidden` (the BSD `UF_HIDDEN`
flag) or, less commonly, by setting the invisible bit in the
`com.apple.FinderInfo` extended attribute. When SAGE ingests such a file,
`shutil.copy2` propagates the BSD chflag to the vault copy, which then
hides from Finder. This is a category error: the bit encodes source-
artifact semantics ("this is scratch, hide it"), not canonical-artifact
semantics — and the vault is the canonical state substrate whose files
must remain user-auditable.

## Propagation mechanisms investigated

Two macOS invisibility mechanisms exist. Their behavior under Python's
`shutil.copy2` on macOS differs:

1. **BSD `UF_HIDDEN` chflag.** Set via `os.chflags(path, stat.UF_HIDDEN)`.
   **Does propagate** via `shutil.copystat -> os.chflags(dst, st.st_flags)`.
   This is the real-world vector we observed in the Cowork scenario.

2. **`com.apple.FinderInfo` invisible bit (0x40 in byte 8).** Set via the
   `xattr` CLI or direct syscall. **Does not propagate** on macOS with
   standard Python, because Python stdlib's `os.listxattr` / `os.getxattr`
   / `os.setxattr` are Linux-only; `shutil._copyxattr` is a no-op on macOS.
   Sanitization here is defensive: guards against future Python versions
   that add macOS xattr support, alternative copy mechanisms, or filesystem
   operations that do propagate FinderInfo.

## Scope of the sanitization step

`IngestionService._ensure_vault_local` gains a post-copy helper that:

- Clears `UF_HIDDEN` from the destination's BSD flags.
- If `com.apple.FinderInfo` xattr exists on the destination, clears bit
  0x40 in byte 8 (invisible), preserving all other bytes.
- Is a no-op on non-macOS platforms and when neither marker is present.
- Swallows errors: UI-layer sanitization must not fail an ingest.

## Test environment

Tests use `tmp_path` for external source files and `tmp_vault_dir` for
the vault. Ingestion is invoked with an absolute path to force the
external-file copy path in `_ensure_vault_local`. macOS-only tests are
gated by `pytest.mark.skipif(sys.platform != "darwin")`.

Test helpers use the macOS `/usr/bin/xattr` CLI to set and read
`com.apple.FinderInfo`, because Python stdlib has no xattr API on macOS.

---

## TEST-SAGE-UIN-001: BSD UF_HIDDEN chflag is cleared on ingest (integration)

**Artifact:** `sage/services/ingestion.py` (`_ensure_vault_local`,
`_strip_ui_invisibility`)
**Category:** ui_normalization
**Decision:** When an external source file has `UF_HIDDEN` set,
the vault copy does not have that flag after ingest.

**Precondition:** Platform is macOS. `os.chflags` available.

**Input:**
- External source file (outside `storage_root`) with `UF_HIDDEN` set
  via `os.chflags`.
- `ingest(IngestRequest(source=<absolute_path>, adapter="markdown"))`.

**Expected:**
- Source file still has `UF_HIDDEN` (unchanged).
- Vault copy at `storage_root/imports/<name>` exists and
  `st_flags & UF_HIDDEN == 0`.

**Rationale:** This is the empirically observed Cowork scenario. Python's
`shutil.copy2` on macOS propagates BSD flags via `os.chflags(dst, st.st_flags)`,
so an agent's hidden-temp file becomes a hidden canonical vault artifact.
The sanitization step must clear this bit on the destination.

---

## TEST-SAGE-UIN-002: FinderInfo invisible bit is cleared by the helper (unit)

**Artifact:** `sage/services/ingestion.py` (`_strip_ui_invisibility`)
**Category:** ui_normalization
**Decision:** When `_strip_ui_invisibility` is called on a file whose
`com.apple.FinderInfo` xattr has the invisible bit (0x40) set in byte 8,
the xattr is updated so that bit is cleared.

**Precondition:** Platform is macOS. `/usr/bin/xattr` present.

**Input:**
- A file with `com.apple.FinderInfo` set to a 32-byte payload where
  byte 8 is 0x40 and all other bytes are 0.
- Direct call: `_strip_ui_invisibility(path)`.

**Expected:**
- Either `com.apple.FinderInfo` is absent after the call (stripped when
  all bytes become zero) or byte 8 has bit 0x40 cleared.

**Rationale:** Defensive. On standard-build macOS Python, `shutil.copy2`
does not propagate this xattr, so the ingestion integration does not
trigger this path. But the sanitization function should still clear it
when present, to guard against alternative copy mechanisms and future
Python versions that may add xattr support on macOS. Tested at the unit
level because a pure integration test (ingest through the pipeline)
would not reliably set up the xattr on the vault copy.

---

## TEST-SAGE-UIN-003: Non-invisibility FinderInfo bytes are preserved (unit)

**Artifact:** `sage/services/ingestion.py` (`_strip_ui_invisibility`)
**Category:** ui_normalization
**Decision:** Sanitization clears only the invisible bit (0x40) in byte 8.
Other bytes of `com.apple.FinderInfo` (type and creator codes, color
labels, stationery flag, etc.) are preserved.

**Precondition:** Platform is macOS.

**Input:**
- A file with `com.apple.FinderInfo` payload where bytes 0-3 are b"TEXT",
  bytes 4-7 are b"ttxt", byte 8 is 0x4C (invisible 0x40 | color label
  mask 0x0C), and the rest are 0.
- Direct call: `_strip_ui_invisibility(path)`.

**Expected:**
- Byte 8 equals 0x0C (invisible cleared, color label preserved).
- Bytes 0-3 unchanged (b"TEXT").
- Bytes 4-7 unchanged (b"ttxt").

**Rationale:** Sanitization must be surgical. Color labels, type codes,
and other Finder metadata are legitimate user-visible properties that
should survive ingestion.

---

## TEST-SAGE-UIN-004: Ingest succeeds when source has no UI markers

**Artifact:** `sage/services/ingestion.py` (`_strip_ui_invisibility`,
`_ensure_vault_local`)
**Category:** resilience
**Decision:** When the source file has no `UF_HIDDEN` chflag and no
`com.apple.FinderInfo` xattr (the common case), the sanitization step
is a no-op and ingestion completes normally.

**Precondition:** Any platform where ingest runs.

**Input:**
- External source file with default flags and no extended attributes.
- `ingest(IngestRequest(source=<absolute_path>, adapter="markdown"))`.

**Expected:**
- Ingestion completes successfully; `result.is_new is True`.
- Vault copy exists.
- No exception is raised by the sanitization step.

**Rationale:** The normal-case path must not be disturbed by the
sanitization logic.
