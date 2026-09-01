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

from sage.vault_source_binding import (
    _SOURCE_CHUNK_BYTES,
    FilesystemVaultSourceStore,
    VaultRootEscapeError,
)

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


def test_vsbb_025_planned_source_path_reports_the_home_without_retaining(
    store, storage_root, tmp_path
):
    """``planned_source_path`` names where a source would be retained, and
    retains nothing.

    Anti-coincidental-pass: the internal case asserts the source's own
    *subdirectory* path, which an implementation returning ``imports/<name>`` for
    everything would fail -- a name-only assertion would not distinguish the two.
    The external case asserts nothing was written, so an implementation that
    answered the question by performing the retain is caught as well.
    """
    internal = storage_root / "reports" / "x.md"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(b"data")
    assert store.planned_source_path(VID, storage_root, internal) == "reports/x.md"

    external = _external(tmp_path, "x.md", b"data")
    assert store.planned_source_path(VID, storage_root, external) == "imports/x.md"

    assert not (storage_root / "imports").exists(), "planning must not retain anything"


def test_vsbb_026_planned_source_path_matches_what_retain_returns(store, storage_root, tmp_path):
    """For a first, uncontested retain the planned path is the path
    ``retain_source`` actually returns, in both the internal and external cases.

    The two must not drift: a caller that skips a redundant retain acts on the
    planned path, so a planned path the binding would not have chosen would
    silently re-home the document.
    """
    internal = storage_root / "reports" / "y.md"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(b"data")
    assert store.planned_source_path(VID, storage_root, internal) == store.retain_source(
        VID, storage_root, internal
    )

    external = _external(tmp_path, "z.md", b"body")
    assert store.planned_source_path(VID, storage_root, external) == store.retain_source(
        VID, storage_root, external
    )


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
# retain_source: caller-supplied delivered digest
# --------------------------------------------------------------------------- #


def test_vsbb_027_retain_with_supplied_digest_matches_the_computed_result(
    store, storage_root, tmp_path
):
    """Handing ``retain_source`` the digest of the delivered bytes produces exactly
    the placement it would have computed for itself.

    The caller has already hashed the file (an ingest needs the digest before it
    can ask whether these bytes are known), so the digest is threaded down rather
    than taken a second time. Threading it must change nothing observable: same
    disambiguation suffix, same bytes at the same path as VSBB-004.
    """
    a = _external(tmp_path, "doc.md", b"alpha")
    store.retain_source(VID, storage_root, a)

    b = _external(tmp_path / "other", "doc.md", b"bravo")
    rel_b = store.retain_source(
        VID, storage_root, b, delivered_hash=f"sha256:{hashlib.sha256(b'bravo').hexdigest()}"
    )

    assert rel_b == f"imports/doc_{hashlib.sha256(b'bravo').hexdigest()[:8]}.md"
    assert (storage_root / rel_b).read_bytes() == b"bravo"


def test_vsbb_028_retain_with_supplied_digest_still_reuses_identical_content(
    store, storage_root, tmp_path
):
    """A supplied digest does not defeat the identical-content reuse rule: the
    second retain of the same bytes returns the first copy's path and writes
    nothing new."""
    first = _external(tmp_path, "dup.md", b"same")
    digest = f"sha256:{hashlib.sha256(b'same').hexdigest()}"
    rel1 = store.retain_source(VID, storage_root, first, delivered_hash=digest)

    second = _external(tmp_path / "again", "dup.md", b"same")
    rel2 = store.retain_source(VID, storage_root, second, delivered_hash=digest)

    assert rel1 == rel2 == "imports/dup.md"
    assert list((storage_root / "imports").glob("dup*.md")) == [storage_root / "imports" / "dup.md"]


def test_vsbb_029_retain_consumes_the_supplied_digest_rather_than_recomputing(
    store, storage_root, tmp_path
):
    """The supplied digest is the one the collision decision uses -- the binding
    trusts its caller and does not re-derive a digest from the bytes.

    Anti-coincidental-pass: this is the only assertion that proves the parameter
    is load-bearing. VSBB-027 passes a *correct* digest, so an implementation
    that accepted the argument and silently recomputed would satisfy it. Here the
    digest is deliberately wrong, so the disambiguation suffix can only carry the
    supplied value if the supplied value was actually read. The
    default-to-computing path stays covered by VSBB-001 through VSBB-005, none of
    which pass a digest at all.
    """
    a = _external(tmp_path, "doc.md", b"alpha")
    store.retain_source(VID, storage_root, a)

    b = _external(tmp_path / "other", "doc.md", b"bravo")
    wrong = f"sha256:{'0' * 64}"
    rel_b = store.retain_source(VID, storage_root, b, delivered_hash=wrong)

    assert rel_b == "imports/doc_00000000.md"
    assert rel_b != f"imports/doc_{hashlib.sha256(b'bravo').hexdigest()[:8]}.md"
    assert (storage_root / rel_b).read_bytes() == b"bravo"


# --------------------------------------------------------------------------- #
# write_source: bytes at a caller-named path
# --------------------------------------------------------------------------- #


def test_vsbb_032_write_source_replaces_bytes_at_the_named_path(store, storage_root, tmp_path):
    """``write_source`` puts the caller's bytes at the path the caller named,
    replacing whatever sat there.

    Anti-coincidental-pass: the sibling-glob assertion is the whole claim. An
    implementation that delegated to ``retain_source`` would also leave the right
    bytes somewhere -- at ``imports/x_<hash8>.md``, disambiguated away from the
    path the caller asked for, with the wrong bytes still at the target. Only
    asserting that no sibling exists distinguishes a write-in-place from a
    collision-handled retain.
    """
    ext = _external(tmp_path, "x.md", b"original")
    rel = store.retain_source(VID, storage_root, ext)
    (storage_root / rel).write_bytes(b"drifted out of band")

    store.write_source(VID, storage_root, rel, b"original")

    assert (storage_root / rel).read_bytes() == b"original"
    assert list((storage_root / "imports").glob("x*.md")) == [storage_root / "imports" / "x.md"]


def test_vsbb_033_write_source_creates_missing_parents(store, storage_root):
    """A named path whose parent directory does not exist yet is written anyway --
    the restore of a copy whose whole tree went missing must not fail on the
    directory."""
    assert not (storage_root / "imports").exists()

    store.write_source(VID, storage_root, "imports/new.md", b"data")

    assert (storage_root / "imports" / "new.md").read_bytes() == b"data"


def test_vsbb_034_write_source_refuses_to_escape_the_storage_root(store, storage_root, tmp_path):
    """A caller-named path that traverses out of the storage root is refused.

    Anti-coincidental-pass: the assertion is on the *filesystem outside the root*,
    not merely on the exception type. A guard that resolved the path but never
    compared it would raise nothing and write the file; a guard that raised after
    writing would satisfy a ``pytest.raises`` check alone.
    """
    outside = storage_root.parent / "escape.md"
    assert not outside.exists()

    with pytest.raises(VaultRootEscapeError):
        store.write_source(VID, storage_root, "../escape.md", b"data")

    assert not outside.exists()


def test_vsbb_035_write_source_refuses_an_absolute_path(store, storage_root, tmp_path):
    """A caller-named path that is absolute is refused on shape, even when it
    points inside the vault's own tree.

    Anti-coincidental-pass: the path must be *inside* ``storage_root`` for this
    test to mean anything. ``storage_root / "/abs/outside"`` discards the left
    operand, so an absolute path pointing elsewhere is already refused by the
    pre-existing realpath containment check and the test would pass with the
    shape check deleted -- proving nothing about it. An absolute path inside the
    root resolves within it, so containment accepts and only the shape check
    refuses. The trailing assertion is that no file appeared at the target.
    """
    inside_absolute = storage_root / "imports" / "abs.md"
    assert not inside_absolute.exists()

    with pytest.raises(VaultRootEscapeError):
        store.write_source(VID, storage_root, str(inside_absolute), b"data")

    assert not inside_absolute.exists()


def test_vsbb_037_retain_source_refuses_a_dangling_symlinked_destination(
    store, storage_root, tmp_path
):
    """Retention refuses to copy through a symlink sitting at the path it chose.

    Anti-coincidental-pass: two properties of the fixture do the work. The link
    is *dangling*, so it reports absent and the collision branch is skipped
    entirely — retention falls through to the write, which is the only path on
    which ``copy2`` would follow it. And its target is *inside* the storage root,
    so the containment resolve accepts and only ``_assert_not_symlinked``
    refuses; an outside target would be caught by containment and leave the
    symlink check unexercised, which is what this test's first version did. The
    assertion is that no file appeared at the link's target, not that an
    exception was raised — a guard that raised after copying would pass a
    raises-only check.

    The no-collision sibling of VSBB-038, which reaches the same guard through
    the disambiguating branch.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    redirected = imports / "someone_elses_copy.md"
    (imports / "note.md").symlink_to(redirected)
    assert not redirected.exists(), "the link must dangle for this to be the uncollided path"

    ext = _external(tmp_path, "note.md", b"ATTACKER BYTES")

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert not redirected.exists(), "the write must not be redirected through the link"


def test_vsbb_038_retain_source_refuses_a_link_at_the_disambiguated_path(
    store, storage_root, tmp_path
):
    """Retention guards the path it actually writes, not merely the one it planned.

    Anti-coincidental-pass: the fixture forces an *ordinary collision* first, so
    retention takes the branch that re-derives its destination. VSBB-037's
    fixture has no collision, so the planned path is the written path there and
    it cannot see this branch at all -- which is exactly how a guard placed on
    the planned path alone passed that test while leaving this write open.

    The disambiguated name is derivable: the token is a deterministic function
    of the delivered bytes, so the link's location is not a guess.

    The link points at a path *inside* the storage root, and that is deliberate:
    an outside target is refused by the containment resolve (VSBB-039's guard),
    so the symlink check would never be the discriminator and this test would
    pass with it deleted. An in-root target resolves within the root, so
    containment accepts and only the symlink check refuses. The assertion is
    that the bytes did not appear at the link's target — a redirected write, not
    an escaped one, which is the narrower hazard the check alone covers.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    (imports / "note.md").write_bytes(b"AN ORDINARY COLLISION")

    delivered = b"DELIVERED BYTES"
    token = hashlib.sha256(delivered).hexdigest()[:8]
    redirected = imports / "someone_elses_copy.md"
    (imports / f"note_{token}.md").symlink_to(redirected)
    assert not redirected.exists()

    ext = _external(tmp_path, "note.md", delivered)

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert not redirected.exists(), "the write must not be redirected through the link"


def test_vsbb_039_retain_source_refuses_a_symlinked_ancestor(store, storage_root, tmp_path):
    """A symlinked ``imports/`` lands the copy outside the tree; containment on
    the final destination catches it.

    Anti-coincidental-pass: the leaf here is an ordinary name, so a symlink check
    that inspects only the destination itself passes and the copy still escapes.
    Only resolving the whole path discriminates. Asserted on the escaped
    location, which is where the bytes would otherwise appear while the returned
    vault-relative path claims they are inside the vault.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (storage_root / "imports").symlink_to(elsewhere)
    ext = _external(tmp_path, "n.md", b"data")

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert not (elsewhere / "n.md").exists()


def test_vsbb_040_retain_source_still_disambiguates_around_an_in_tree_link(
    store, storage_root, tmp_path
):
    """A link at the *planned* path whose target is a real in-tree file still
    collision-handles, exactly as before the guards landed.

    The guards must not narrow this. Retention never writes through the link on
    this branch -- it disambiguates to a fresh name -- so refusing here would
    reject a safe, long-standing path for no gain. This is the regression test
    for that over-refusal: it fails against a guard placed on the planned path
    rather than on the written one.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    real = imports / "real.md"
    real.write_bytes(b"ALPHA")
    (imports / "doc.md").symlink_to(real)

    ext = _external(tmp_path, "doc.md", b"BRAVO")
    rel = store.retain_source(VID, storage_root, ext)

    expected = hashlib.sha256(b"BRAVO").hexdigest()[:8]
    assert rel == f"imports/doc_{expected}.md"
    assert (storage_root / rel).read_bytes() == b"BRAVO"
    assert real.read_bytes() == b"ALPHA", "the link's target must be untouched"


def test_vsbb_043_retain_source_refuses_to_reuse_a_symlinked_path(store, storage_root, tmp_path):
    """Retention will not hand back a symlink as a document's retained path.

    The third exit of the method, and the one no other fixture enters. The link
    is *live* and its target already holds the bytes being ingested, so the
    collision comparison matches and retention takes its reuse branch — which
    returns without writing anything, and so reaches neither the write-site
    symlink check nor the containment resolve.

    Anti-coincidental-pass: the target must hold the *identical* bytes, or the
    disambiguating branch runs instead and VSBB-040's behaviour is what gets
    exercised. Nothing is written on this path, so a byte-level assertion cannot
    discriminate; what the test asserts instead is that no path was returned at
    all. The defect it excludes is a returned ``imports/note.md`` that is a
    symlink resolving out of the tree: every read would follow it wherever its
    owner points, and a later repair would refuse the very path the record
    names, leaving a document that cannot be restored where it claims to live.
    The link is asserted intact afterwards, since refusing must not mutate the
    tree either.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    body = b"THE BYTES BEING INGESTED"
    outside = tmp_path / "outside_target.md"
    outside.write_bytes(body)
    (imports / "note.md").symlink_to(outside)

    ext = _external(tmp_path, "note.md", body)

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert (imports / "note.md").is_symlink(), "a refusal must not disturb the tree"
    assert outside.read_bytes() == body


def test_vsbb_036_write_source_refuses_a_symlinked_destination(store, storage_root, tmp_path):
    """A symlink at the named path is refused rather than followed.

    Anti-coincidental-pass: containment alone does not catch this, and asserting
    only that an exception was raised would not show why. The link's target sits
    *inside* the source root, so it passes the realpath containment check that
    VSBB-034 exercises; the write would land on the target while the named path
    stayed drifted, silently overwriting a second document's retained copy. The
    victim's bytes are the assertion. The operation's precondition is that
    something other than SAGE wrote to the store, so a planted link is in scope.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    victim = imports / "victim.md"
    victim.write_bytes(b"VICTIM ORIGINAL")
    link = imports / "link.md"
    link.symlink_to(victim)

    with pytest.raises(VaultRootEscapeError):
        store.write_source(VID, storage_root, "imports/link.md", b"ATTACKER BYTES")

    assert victim.read_bytes() == b"VICTIM ORIGINAL", "the link's target must be untouched"
    assert link.is_symlink()


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
