"""Source-byte half of the vault-source-store filesystem binding (CAS-ADR-043).

The store owns the source files retained from each ingest, not just the vault
configuration declaration. These tests exercise the filesystem binding's
source-byte operations directly: retention on ingest (the ``imports/`` copy with
content-hash collision handling and CAS-ADR-016 UI-invisibility stripping) and
read-back for delivery and integrity audit (existence, size, bytes, hash).

Test IDs follow VSBB-NNN (Vault-Source Binding Bytes). ``vault_id`` is passed but
ignored by the filesystem binding, which addresses sources by ``storage_root`` /
``source_path``; it is carried for the document-store binding that keys sources
by vault.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from sage.vault_source_binding import _SOURCE_CHUNK_BYTES, FilesystemVaultSourceStore

VID = "vault_x"  # ignored by the filesystem binding; present for signature parity.

requires_macos = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="UI-layer invisibility markers are macOS-specific",
)


@pytest.fixture
def storage_root(tmp_path):
    """A per-vault source root with the binding under test."""
    root = tmp_path / "sources"
    root.mkdir()
    return root


@pytest.fixture
def store():
    return FilesystemVaultSourceStore(Path("/unused/vault_root"))


def _external(tmp_path, name, body: bytes) -> Path:
    """Create a file OUTSIDE the storage root (an external ingest source)."""
    ext = tmp_path / "agent_workspace" / name
    ext.parent.mkdir(parents=True, exist_ok=True)
    ext.write_bytes(body)
    return ext


# --------------------------------------------------------------------------- #
# retain_source
# --------------------------------------------------------------------------- #


def test_vsbb_001_retain_external_copies_into_imports(store, storage_root, tmp_path):
    """An external source is copied into ``imports/`` and its byte copy lands
    under the storage root; the returned path is vault-relative.

    Anti-coincidental: assert the copied bytes exist at the returned path, not
    merely that a string came back."""
    ext = _external(tmp_path, "note.md", b"# Note\n")
    rel = store.retain_source(VID, storage_root, ext)

    assert rel == "imports/note.md"
    assert (storage_root / rel).read_bytes() == b"# Note\n"


def test_vsbb_002_retain_internal_returns_relative_without_copy(store, storage_root):
    """A source already under the storage root is retained in place: its
    vault-relative path is returned and no ``imports/`` copy is made."""
    internal = storage_root / "reports" / "x.md"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(b"data")

    rel = store.retain_source(VID, storage_root, internal)

    assert rel == "reports/x.md"
    assert not (storage_root / "imports").exists()


def test_vsbb_003_retain_collision_identical_content_reuses(store, storage_root, tmp_path):
    """Retaining the same external bytes under the same basename twice reuses the
    first copy (no suffix), because the content hash matches."""
    first = _external(tmp_path, "dup.md", b"same")
    rel1 = store.retain_source(VID, storage_root, first)

    second = _external(tmp_path / "again", "dup.md", b"same")
    rel2 = store.retain_source(VID, storage_root, second)

    assert rel1 == rel2 == "imports/dup.md"
    assert list((storage_root / "imports").glob("dup*.md")) == [storage_root / "imports" / "dup.md"]


def test_vsbb_004_retain_collision_different_content_suffixes(store, storage_root, tmp_path):
    """Two different files sharing a basename: the second is disambiguated with an
    8-char content-hash suffix, and both byte copies are present (BH-055)."""
    a = _external(tmp_path, "doc.md", b"alpha")
    rel_a = store.retain_source(VID, storage_root, a)

    b = _external(tmp_path / "other", "doc.md", b"bravo")
    rel_b = store.retain_source(VID, storage_root, b)

    expected_hash = hashlib.sha256(b"bravo").hexdigest()[:8]
    assert rel_a == "imports/doc.md"
    assert rel_b == f"imports/doc_{expected_hash}.md"
    assert (storage_root / rel_a).read_bytes() == b"alpha"
    assert (storage_root / rel_b).read_bytes() == b"bravo"


def test_vsbb_005_retain_creates_missing_imports_dir(store, storage_root, tmp_path):
    """The ``imports/`` directory is created on demand when absent."""
    assert not (storage_root / "imports").exists()
    ext = _external(tmp_path, "fresh.md", b"hi")

    rel = store.retain_source(VID, storage_root, ext)

    assert (storage_root / "imports").is_dir()
    assert (storage_root / rel).read_bytes() == b"hi"


# --------------------------------------------------------------------------- #
# read-back: exists / size / read / hash
# --------------------------------------------------------------------------- #


def test_vsbb_006_read_source_round_trips_bytes(store, storage_root, tmp_path):
    """``read_source`` returns the exact retained bytes."""
    ext = _external(tmp_path, "payload.md", b"\x00\x01binary-ish\xff")
    rel = store.retain_source(VID, storage_root, ext)

    assert store.read_source(VID, storage_root, rel) == b"\x00\x01binary-ish\xff"


def test_vsbb_007_source_exists_true_and_false(store, storage_root, tmp_path):
    """``source_exists`` is True for a retained path and False for a missing one."""
    ext = _external(tmp_path, "here.md", b"present")
    rel = store.retain_source(VID, storage_root, ext)

    assert store.source_exists(VID, storage_root, rel) is True
    assert store.source_exists(VID, storage_root, "imports/absent.md") is False


def test_vsbb_008_source_size_matches_byte_length(store, storage_root, tmp_path):
    """``source_size`` equals the byte length of the retained source."""
    body = b"twelve bytes"
    ext = _external(tmp_path, "sized.md", body)
    rel = store.retain_source(VID, storage_root, ext)

    assert store.source_size(VID, storage_root, rel) == len(body)


def test_vsbb_009_hash_source_canonical_sha256(store, storage_root, tmp_path):
    """``hash_source`` returns ``sha256:<hex>`` over the retained bytes, matching a
    direct digest (streamed, so a large file is never loaded whole)."""
    body = b"x" * (65536 * 2 + 7)  # spans multiple read chunks
    ext = _external(tmp_path, "big.md", body)
    rel = store.retain_source(VID, storage_root, ext)

    assert store.hash_source(VID, storage_root, rel) == f"sha256:{hashlib.sha256(body).hexdigest()}"


def test_vsbb_011_iter_source_streams_in_bounded_chunks(store, storage_root, tmp_path):
    """``iter_source`` yields the retained bytes in chunks no larger than the
    delivery chunk size, so a large source is never materialized whole.

    Anti-coincidental: a 150 000-byte body must arrive in at least three chunks
    -- an implementation that reads the file whole (``read_bytes`` or a
    ``read_source`` delegation) yields a single oversized chunk and fails both
    the count and the per-chunk bound."""
    body = os.urandom(150_000)  # crosses the 65536 chunk boundary twice
    ext = _external(tmp_path, "big.bin", body)
    rel = store.retain_source(VID, storage_root, ext)

    chunks = list(store.iter_source(VID, storage_root, rel))

    assert b"".join(chunks) == body
    assert len(chunks) >= 3
    assert all(len(chunk) <= _SOURCE_CHUNK_BYTES for chunk in chunks)


def test_vsbb_012_iter_source_missing_file_raises(store, storage_root):
    """Consuming ``iter_source`` for an absent path raises ``FileNotFoundError``,
    matching ``read_source``'s behavior for the same miss.

    Anti-coincidental: a generator that silently yields nothing for a missing
    file would pass a join-only check; the raises-check catches fail-open."""
    with pytest.raises(FileNotFoundError):
        list(store.iter_source(VID, storage_root, "imports/absent.bin"))


# --------------------------------------------------------------------------- #
# CAS-ADR-016 UI-invisibility stripping on retain (macOS)
# --------------------------------------------------------------------------- #


@requires_macos
def test_vsbb_010_retain_strips_ui_invisibility(store, storage_root, tmp_path):
    """Retaining an external source carrying the BSD UF_HIDDEN chflag clears it on
    the vault copy while leaving the source untouched (CAS-ADR-016)."""
    ext = _external(tmp_path, "hidden.md", b"# Hidden\n")
    os.chflags(str(ext), stat.UF_HIDDEN)
    assert os.lstat(str(ext)).st_flags & stat.UF_HIDDEN != 0

    rel = store.retain_source(VID, storage_root, ext)

    # Source unchanged; vault copy sanitized.
    assert os.lstat(str(ext)).st_flags & stat.UF_HIDDEN != 0
    assert os.lstat(str(storage_root / rel)).st_flags & stat.UF_HIDDEN == 0
