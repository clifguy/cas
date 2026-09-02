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

    The reuse exit, and the one no other fixture enters. The link is *live* and
    its target already holds the bytes being ingested, so the collision
    comparison matches and retention reuses the path rather than writing.

    Anti-coincidental-pass: two properties of the fixture do the work. The
    target must hold the *identical* bytes, or the disambiguating branch runs
    instead and VSBB-040's behaviour is what gets exercised. And the target sits
    *inside* the storage root, so the containment resolve accepts and only
    ``_assert_not_symlinked`` refuses; an outside target would be caught by
    containment and leave the symlink check unexercised — which is what this
    test's first version did, exactly as VSBB-037's did before it.

    The in-tree case is also the consequential one. The defect excluded is a
    returned ``imports/note.md`` that is a symlink onto a *second document's*
    retained copy: every read of this record would land on that document's
    bytes, and a later repair would write over them at a path this record
    claims as its own. Nothing is written on this exit, so the assertions are
    that no path came back, that the link survives, and that the copy it points
    at is untouched.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    body = b"THE BYTES BEING INGESTED"
    someone_elses_copy = imports / "someone_elses_copy.md"
    someone_elses_copy.write_bytes(body)
    (imports / "note.md").symlink_to(someone_elses_copy)

    ext = _external(tmp_path, "note.md", body)

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert (imports / "note.md").is_symlink(), "a refusal must not disturb the tree"
    assert someone_elses_copy.read_bytes() == body


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


def test_vsbb_052_retain_source_refuses_a_directory_at_the_planned_path(
    store, storage_root, tmp_path
):
    """A directory sitting where retention planned to write is refused in the
    binding's own vocabulary, and the refusal names the vault-relative path.

    The planned path is hashed to decide between reuse and disambiguation
    before any guard runs, so a directory there escaped as a bare
    ``IsADirectoryError`` from the hash.

    Anti-coincidental-pass: the exception class discriminates against the bare
    error, and the "no disambiguated file" assertion excludes a rival that
    catches it and falls through to the collision branch's fresh name. The
    message half asserts the vault-relative spelling *and* the absence of the
    storage root, since the absolute form ends in the same suffix and the
    positive assertion alone would pass against it.
    """
    imports = storage_root / "imports"
    squatter = imports / "note.md"
    squatter.mkdir(parents=True)
    delivered = b"DELIVERED BYTES"
    ext = _external(tmp_path, "note.md", delivered)

    with pytest.raises(VaultRootEscapeError) as excinfo:
        store.retain_source(VID, storage_root, ext)

    message = str(excinfo.value)
    assert "imports/note.md" in message
    assert str(storage_root) not in message
    assert squatter.is_dir()
    assert list(squatter.iterdir()) == [], "nothing may be copied into the directory"
    token = hashlib.sha256(delivered).hexdigest()[:8]
    assert not (imports / f"note_{token}.md").exists(), "no fall-through to disambiguation"


def test_vsbb_053_retain_source_refuses_a_dangling_imports_link(store, storage_root, tmp_path):
    """A dangling symlink where ``imports/`` should be is refused rather than
    escaping as the bare ``FileExistsError`` its ``mkdir`` raises.

    Anti-coincidental-pass: the link must *dangle*. A live link to a directory
    passes ``mkdir(exist_ok=True)`` and is refused later by containment
    (VSBB-039), so it would leave this branch unexercised. The link target's
    continued absence excludes an implementation that created the directory
    through the link and wrote under it.
    """
    gone = tmp_path / "gone"
    (storage_root / "imports").symlink_to(gone)
    assert not gone.exists(), "the link must dangle for the mkdir to be the failing step"
    ext = _external(tmp_path, "n.md", b"data")

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert not gone.exists(), "nothing may be created through the dangling link"
    assert (storage_root / "imports").is_symlink(), "a refusal must not disturb the tree"


def test_vsbb_054_retain_source_refuses_a_directory_at_the_disambiguated_path(
    store, storage_root, tmp_path
):
    """A directory at the name the collision branch re-derives is refused, not
    copied into.

    Anti-coincidental-pass: the collision at the planned path must be real
    (different bytes), or the planned-path check fires first and the settled
    destination is never examined -- the same trap VSBB-038 sets for the
    symlink guard. ``shutil.copy2`` to a directory raises nothing: it copies
    *into* it under the source's basename, and retention would then return the
    directory as the record's ``source_path``. The assertion is therefore that
    the directory is still empty, not merely that an exception was raised.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    (imports / "note.md").write_bytes(b"AN ORDINARY COLLISION")
    delivered = b"DELIVERED BYTES"
    token = hashlib.sha256(delivered).hexdigest()[:8]
    squatter = imports / f"note_{token}.md"
    squatter.mkdir()
    ext = _external(tmp_path, "note.md", delivered)

    with pytest.raises(VaultRootEscapeError):
        store.retain_source(VID, storage_root, ext)

    assert list(squatter.iterdir()) == [], "the copy must not land inside the directory"
    assert (imports / "note.md").read_bytes() == b"AN ORDINARY COLLISION"


def test_vsbb_055_write_source_refuses_a_directory_at_the_named_path(store, storage_root):
    """A directory at the path a repair names is refused in the binding's own
    vocabulary rather than escaping as the bare ``IsADirectoryError`` of the
    write, and the refusal names the vault-relative path.

    The operation's precondition is that something other than SAGE wrote to the
    store, so a directory planted at a record's path is in scope.

    Anti-coincidental-pass: the directory sits *inside* the root and is no
    symlink, so containment and the symlink guard both accept and only the
    directory check refuses. The message's negative half is the discriminator
    against the absolute spelling, which ends in the same path.
    """
    squatter = storage_root / "imports" / "doc.md"
    squatter.mkdir(parents=True)

    with pytest.raises(VaultRootEscapeError) as excinfo:
        store.write_source(VID, storage_root, "imports/doc.md", b"x")

    message = str(excinfo.value)
    assert "imports/doc.md" in message
    assert str(storage_root) not in message
    assert list(squatter.iterdir()) == [], "nothing may be written into the directory"


def test_vsbb_056_write_source_refuses_a_file_where_a_parent_directory_belongs(store, storage_root):
    """A regular file sitting where the named path's parent directory belongs is
    refused rather than escaping as the bare ``FileExistsError`` of the parent
    ``mkdir``.

    Anti-coincidental-pass: a *dangling link* at the parent would not reach the
    ``mkdir`` -- the containment resolve follows it and either refuses an
    outside target or resolves an inside one to a plain path -- so only a
    regular file exercises this branch. The file's bytes are asserted intact,
    excluding an implementation that replaced it with the directory it wanted.
    """
    squatter = storage_root / "imports"
    squatter.write_bytes(b"NOT A DIRECTORY")

    with pytest.raises(VaultRootEscapeError):
        store.write_source(VID, storage_root, "imports/doc.md", b"x")

    assert squatter.read_bytes() == b"NOT A DIRECTORY"


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


def test_vsbb_057_source_is_symlink_discriminates_a_link_from_a_regular_file(
    store, storage_root, tmp_path
):
    """``source_is_symlink`` reports the path's own nature, not its target's.

    The read side's blind spot: something other than SAGE replaces a retained
    copy with a link to a file holding the expected bytes. Every read follows
    the link, so the record names a path the write side will refuse and nothing
    surfaces it.

    Anti-coincidental-pass: the link's target exists and holds valid bytes, so
    an implementation built on ``exists()`` -- which is what ``source_exists``
    is -- answers True for both inputs and discriminates nothing. Only an
    ``lstat`` separates them. Both directions are asserted, so a constant answer
    fails whichever value it is.
    """
    ext = _external(tmp_path, "plain.md", b"ordinary")
    plain_rel = store.retain_source(VID, storage_root, ext)

    target = storage_root / "imports" / "target.md"
    target.write_bytes(b"the expected bytes")
    (storage_root / "imports" / "linked.md").symlink_to(target)

    assert store.source_is_symlink(VID, storage_root, "imports/linked.md") is True
    assert store.source_is_symlink(VID, storage_root, plain_rel) is False
    assert store.source_exists(VID, storage_root, "imports/linked.md") is True, (
        "the link must resolve, or the existence check alone would catch it"
    )


def test_vsbb_058_source_is_symlink_is_true_for_a_dangling_link(store, storage_root):
    """A link whose target is absent is still a link at the recorded path.

    It matters because the audit must report such a path as linked rather than
    merely missing: the repair primitive refuses to write through a link, so
    reporting it missing would send an operator to a repair that then refuses.

    Anti-coincidental-pass: ``source_exists`` is False here, so an
    implementation that folds presence into its answer (``exists and
    is_symlink``) returns False and fails. The existence assertion below is what
    makes that rival visible rather than merely excluded.
    """
    imports = storage_root / "imports"
    imports.mkdir()
    (imports / "dangling.md").symlink_to(imports / "never_written.md")

    assert store.source_is_symlink(VID, storage_root, "imports/dangling.md") is True
    assert store.source_exists(VID, storage_root, "imports/dangling.md") is False


def test_vsbb_059_paths_the_binding_itself_produced_are_never_symlinked(
    store, storage_root, tmp_path
):
    """Neither of the binding's own write paths leaves a link behind.

    The unchanged-behaviour half of the contract: the new observation must not
    begin reporting ordinary retained files as linked. Both write paths are
    covered because both are guarded by the same refusal, and a regression in
    either would be invisible to a test exercising only one.

    Anti-coincidental-pass: asserted against paths the binding chose, so an
    implementation returning a constant True fails here while VSBB-057 fails a
    constant False. An absent path is asserted too -- ``is_symlink`` on nothing
    is False, not an error.
    """
    ext = _external(tmp_path, "retained.md", b"copied in")
    retained_rel = store.retain_source(VID, storage_root, ext)
    store.write_source(VID, storage_root, "imports/written.md", b"put here")

    assert store.source_is_symlink(VID, storage_root, retained_rel) is False
    assert store.source_is_symlink(VID, storage_root, "imports/written.md") is False
    assert store.source_is_symlink(VID, storage_root, "imports/absent.md") is False


def test_vsbb_061_a_regular_file_under_a_symlinked_ancestor_is_not_symlinked(
    store, storage_root, tmp_path
):
    """The question is about the recorded path's own final component, not about
    whether reaching it crosses a link.

    A vault whose ``imports/`` is itself a link is an ordinary deployment shape
    (the sources moved to another volume), and every retained copy under it is
    still a regular file. Reporting those as linked would take a whole vault's
    documents red on an audit and refuse every repair, for a condition none of
    them has.

    Anti-coincidental-pass: this is the case that separates an ``lstat`` of the
    final component from the rival that asks whether the path resolves to
    somewhere other than itself. Both answer the plain-file and dangling-link
    cases identically, so VSBB-057 through 059 pass against either; only a link
    in an *ancestor* -- where the leaf is a genuine file but the resolution
    moves -- tells them apart. The ancestor is asserted to be a link and the
    leaf asserted to be a real file, so a fixture that failed to build the
    condition cannot pass by accident.
    """
    real_imports = tmp_path / "elsewhere" / "imports"
    real_imports.mkdir(parents=True)
    (real_imports / "doc.md").write_bytes(b"an ordinary retained copy")
    (storage_root / "imports").symlink_to(real_imports)

    assert (storage_root / "imports").is_symlink(), "the ancestor must really be a link"
    assert (storage_root / "imports" / "doc.md").is_file(), "the leaf must really be a file"

    assert store.source_is_symlink(VID, storage_root, "imports/doc.md") is False


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
