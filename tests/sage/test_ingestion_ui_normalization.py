"""Ingestion UI-layer metadata normalization tests (UIN-001 through UIN-004).

Validates CAS-ADR-016: SAGE strips macOS UI-invisibility markers
(UF_HIDDEN chflag, com.apple.FinderInfo invisible bit) from files
copied into a vault during ingestion, so that canonical artifacts
remain user-auditable regardless of source-file UI state.

See tests/sage/ingestion_ui_normalization_tests.md for the full spec.

Empirical finding (probe on macOS + CPython 3.12):
  * shutil.copy2 DOES propagate BSD UF_HIDDEN via os.chflags.
  * shutil.copy2 does NOT propagate com.apple.FinderInfo, because
    Python stdlib has no xattr API on macOS (os.listxattr et al are
    Linux-only), so shutil._copyxattr is a no-op.

Accordingly:
  * UIN-001 is an integration test on the real propagation vector
    (chflag), invoked through the full ingest pipeline.
  * UIN-002 and UIN-003 are unit tests on the sanitization helper,
    exercising the defensive xattr path that integration would not
    trigger on standard-build macOS Python.

Test helpers use the macOS /usr/bin/xattr CLI to set and read
com.apple.FinderInfo.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import _strip_ui_invisibility

# ---------------------------------------------------------------------------
# macOS-only tests gate
# ---------------------------------------------------------------------------

requires_macos = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="UI-layer invisibility markers are macOS-specific",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_external_source(tmp_path: Path, name: str, body: str) -> Path:
    """Create a source file OUTSIDE any vault storage_root.

    Using tmp_path (the top-level pytest temp dir) ensures _ensure_vault_local
    treats the file as external and invokes shutil.copy2 into imports/.
    """
    path = tmp_path / "agent_workspace" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _set_finder_info(path: Path, data: bytes) -> None:
    """Set com.apple.FinderInfo via /usr/bin/xattr (test-side only)."""
    subprocess.run(
        ["xattr", "-wx", "com.apple.FinderInfo", data.hex(), str(path)],
        check=True,
        capture_output=True,
    )


def _get_finder_info(path: Path) -> bytes | None:
    """Read com.apple.FinderInfo via /usr/bin/xattr; None if absent."""
    result = subprocess.run(
        ["xattr", "-px", "com.apple.FinderInfo", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    hex_str = result.stdout.replace("\n", "").replace(" ", "")
    if not hex_str:
        return None
    return bytes.fromhex(hex_str)


# ---------------------------------------------------------------------------
# TEST-SAGE-UIN-001: BSD UF_HIDDEN chflag is cleared on ingest (integration)
# ---------------------------------------------------------------------------


@requires_macos
async def test_uin_001_bsd_hidden_chflag_cleared_on_ingest(
    tmp_path, tmp_vault_dir, ingestion_service
):
    src = _write_external_source(tmp_path, "hidden_note.md", "# Hidden\n\nA.")
    os.chflags(str(src), stat.UF_HIDDEN)

    # Sanity: flag is set on the source.
    assert os.lstat(str(src)).st_flags & stat.UF_HIDDEN != 0

    request = IngestRequest(
        source=str(src),  # absolute path forces the external-copy path
        source_type=SourceType.MARKDOWN,
    )
    await ingestion_service.ingest(request)

    # Source unchanged.
    assert os.lstat(str(src)).st_flags & stat.UF_HIDDEN != 0

    # Vault copy sanitized.
    vault_copy = tmp_vault_dir / "sources" / "imports" / "hidden_note.md"
    assert vault_copy.exists()
    assert os.lstat(str(vault_copy)).st_flags & stat.UF_HIDDEN == 0


# ---------------------------------------------------------------------------
# TEST-SAGE-UIN-002: FinderInfo invisible bit cleared by the helper (unit)
# ---------------------------------------------------------------------------


@requires_macos
def test_uin_002_finderinfo_invisible_bit_cleared_by_helper(tmp_path):
    path = tmp_path / "invisible.md"
    path.write_text("hi")

    # Set FinderInfo with invisible bit (0x40 in byte 8) and all else zero.
    payload = bytearray(32)
    payload[8] = 0x40
    _set_finder_info(path, bytes(payload))

    # Sanity.
    info = _get_finder_info(path)
    assert info is not None and (info[8] & 0x40) != 0

    _strip_ui_invisibility(path)

    info = _get_finder_info(path)
    # Either removed entirely (all-zero after clearing) or bit cleared.
    assert info is None or (info[8] & 0x40) == 0


# ---------------------------------------------------------------------------
# TEST-SAGE-UIN-003: Non-invisibility FinderInfo bytes preserved (unit)
# ---------------------------------------------------------------------------


@requires_macos
def test_uin_003_other_finderinfo_bytes_preserved(tmp_path):
    path = tmp_path / "tagged.md"
    path.write_text("hi")

    # bytes 0-3: type "TEXT"; 4-7: creator "ttxt";
    # byte 8: 0x4C (invisible 0x40 | color label 0x0C); rest zero.
    payload = bytearray(32)
    payload[0:4] = b"TEXT"
    payload[4:8] = b"ttxt"
    payload[8] = 0x4C
    _set_finder_info(path, bytes(payload))

    _strip_ui_invisibility(path)

    info = _get_finder_info(path)
    assert info is not None, "FinderInfo should still exist (non-zero bytes)"
    assert info[8] == 0x0C, "Invisible bit cleared, color label preserved"
    assert info[0:4] == b"TEXT"
    assert info[4:8] == b"ttxt"


# ---------------------------------------------------------------------------
# TEST-SAGE-UIN-004: Ingest succeeds when source has no UI markers
# ---------------------------------------------------------------------------


async def test_uin_004_ingest_succeeds_without_ui_markers(
    tmp_path, tmp_vault_dir, ingestion_service
):
    """Cross-platform: the sanitization step must be a no-op when no
    invisibility markers are present. Verifies ingestion is undisturbed."""
    src = _write_external_source(tmp_path, "plain_note.md", "# Plain\n\nA.")

    request = IngestRequest(
        source=str(src),
        source_type=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    assert result.is_new is True
    vault_copy = tmp_vault_dir / "sources" / "imports" / "plain_note.md"
    assert vault_copy.exists()
