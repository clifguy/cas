"""Vault-source-store binding for the SAGE deployment profiles (CAS-ADR-043).

The vault-source store is the durable seam CAS-ADR-042 left bound to the local
filesystem: it persists the vault's configuration declaration
(``vault_config.yaml``) and the source files retained from each ingest. On the
on-box target the filesystem is durable; under a stateless deployment it is
ephemeral, so a vault's config and sources are lost on restart even though the
graph and content stores persist. CAS-ADR-043 promotes this seam to a port on
the same footing as the storage port, with a filesystem binding (today's
behavior) and a tenant-native document-store binding selectable per profile.

This slice carries the **config + discovery** half of the port (discover, load,
write, delete the vault configuration declaration) with the filesystem binding
fully implemented and the document-store binding stubbed; the source-byte
retention/delivery half lands with the concrete document-store adapter, per the
ADR's "established thin and extended as the binding lands" clause.

:func:`build_stack_vault_source_store` is the dispatch between bindings.
Mirroring the storage provisioner's dispatch contract, a test environment
override (``SAGE_TEST_VAULT_SOURCE_BACKEND``) is consulted before the config
key, so the test suite can pin the binding while the committed configuration
selects another.

This module sits in the wiring layer (alongside ``sage.storage_binding``): it
imports the vault-config helpers and the stack config, and nothing below it
imports it back.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import logging
import os
import shutil
import stat
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from sage.config import (
    SageCoreConfig,
    StackDocumentStoreConfig,
    VaultConfig,
    warn_on_retired_sections,
)

logger = logging.getLogger(__name__)

# Read size for streamed source hashing, so a large source file is never
# loaded whole into memory to compute its digest.
_HASH_CHUNK_BYTES = 65536

# Chunk size for streamed source delivery (``iter_source``). Hashing and
# delivery are separate concerns that happen to share a size today; the two
# constants keep them independently tunable.
_SOURCE_CHUNK_BYTES = 65536

# Environment override for the vault-source-backend dispatch, consulted before
# the stack config's ``vault_source_backend`` key, so the test suite can pin
# the filesystem binding process-wide while a committed cloud config selects
# the document store.
VAULT_SOURCE_BACKEND_ENV_VAR = "SAGE_TEST_VAULT_SOURCE_BACKEND"

_VALID_BACKENDS = ("filesystem", "document_store")


class VaultRootEscapeError(ValueError):
    """A filesystem target resolved outside the root it was required to stay under.

    Raised by :func:`resolve_and_assert_within_root` when a path a destructive or
    overwriting operation is about to act on does not realpath-resolve to a
    strict descendant of the root it was checked against -- the bound vault root
    for a teardown, the vault's source root for a caller-named write. A
    ``ValueError`` subclass so an existing ``except ValueError`` handler routes
    it like any other shape failure.
    """


def resolve_and_assert_within_root(
    path: Path, vault_root: Path, *, display: str | None = None
) -> Path:
    """Realpath-resolve ``path`` and require it to be a strict descendant of ``vault_root``.

    The safety primitive behind every vault-teardown ``rmtree``: a vault's
    ``storage_root`` / ``brain_root`` / config path come from its
    (operator- or typo-authorable) configuration, so before any recursive delete
    each is resolved through symlinks and asserted to live *strictly under* the
    root it is checked against -- the bound vault root for a teardown
    (CAS-ADR-043's ``get_vault_root``), the vault's source root for a retained
    copy. A path that resolves outside that root -- or to the root itself, since
    removing it would destroy everything beneath -- raises
    :class:`VaultRootEscapeError` and nothing is written or deleted. Returns the
    resolved path on success so the caller removes the canonical (symlink-free)
    target. ``path`` need not exist: an already-absent target still resolves,
    keeping the teardown idempotent.

    The message is shaped for whoever will read it. Without ``display`` it is
    the operator-facing form: it names the path, its resolution, and the root
    that was checked, because a teardown's receipt and its stderr carry it
    verbatim and a fixed root would misdescribe every use but the first. With
    ``display`` -- the vault-relative spelling the caller supplied or would
    recognize -- the message names only that spelling: a write refusal reaches
    API callers verbatim through the service layer's translation, and an
    absolute server path there discloses the host's filesystem layout to
    whoever tripped it while telling them nothing they can act on.
    """
    resolved = path.expanduser().resolve()
    root = vault_root.expanduser().resolve()
    if resolved == root or root not in resolved.parents:
        if display is not None:
            raise VaultRootEscapeError(
                f"refusing to write {display!r}: it resolves outside the vault's source tree."
            )
        raise VaultRootEscapeError(
            f"refusing to operate on {path} (resolved {resolved}): it is not a "
            f"strict descendant of {root}."
        )
    return resolved


def hash_file(path: Path) -> str:
    """SHA-256 of a local file in canonical ``sha256:<hex>`` form.

    Streamed at ``_HASH_CHUNK_BYTES`` so a large file is never loaded whole into
    memory. The one implementation behind both the filesystem binding's
    ``hash_source`` and the ingestion path's at-receipt hash of a caller's file,
    so the digest a caller can compute locally and the digest SAGE records are
    produced the same way.
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _assert_plain_vault_relative(source_path: str) -> None:
    """Require ``source_path`` to be a plain relative path inside the vault.

    The shape check every binding owes :meth:`VaultSourceStore.write_source`,
    which takes its destination from the caller rather than deriving one. An
    absolute path names somewhere outside the vault's tree outright; a ``..``
    component walks out of it. Neither is expressible as a retained source's
    path, so both are refused before any binding-specific handling.

    Shared rather than left to each binding because only one of them has a local
    tree to resolve against: the document-store binding addresses sources by
    vault id and would otherwise hand the segments to a URL builder that joins
    them verbatim, giving the port's containment promise no implementation at
    all on that side.
    """
    candidate = PurePosixPath(source_path)
    if candidate.is_absolute() or Path(source_path).is_absolute():
        raise VaultRootEscapeError(
            f"refusing to write {source_path!r}: a retained source's path is "
            f"vault-relative, and this one is absolute."
        )
    if ".." in candidate.parts:
        raise VaultRootEscapeError(
            f"refusing to write {source_path!r}: the path walks out of the vault's source tree."
        )


def _assert_not_symlinked(dest: Path, display: str) -> None:
    """Refuse a destination that is a symlink rather than writing through it.

    Every write the filesystem binding performs lands at a path it chose or was
    handed, and in both cases a link sitting there redirects the bytes somewhere
    the binding did not intend: to a second document's retained copy when the
    target is inside the tree, or -- for a *dangling* link, which reports as
    absent so no collision handling engages -- to a file created wherever the
    link points, outside the vault entirely, while the returned vault-relative
    path claims the bytes are inside it.

    Realpath containment does not cover this on its own. A link into the tree
    resolves within the root and passes; a dangling one has nothing to resolve
    against yet. Shared by both write paths so the two cannot drift apart: the
    hazard is a property of writing at a filesystem path, not of either method's
    particular reason for choosing one.

    ``display`` is the vault-relative spelling the refusal names. Every caller
    of this guard is writing on a caller's behalf, so the spelling is required
    rather than optional: the message travels to that caller verbatim, and an
    absolute path in it would disclose the host's layout without telling the
    caller anything it could relate to what it sent.
    """
    if dest.is_symlink():
        raise VaultRootEscapeError(
            f"refusing to write {display!r}: the destination is a symlink, so the "
            f"write would land on its target rather than at the path named."
        )


def _assert_not_directory(dest: Path, display: str) -> None:
    """Refuse a destination that is a directory rather than hashing or writing at it.

    A directory (or a link to one) where a retained copy belongs is not a
    collision retention can disambiguate around: hashing it fails outright,
    and ``shutil.copy2`` into it raises nothing -- it lands the bytes *under*
    the directory, and the record would then name the directory as its own
    source. Checked wherever a destination is about to be read or written,
    which for retention is twice: the planned path, before the collision
    comparison hashes it, and the settled path, before the copy lands there.
    ``display`` is the vault-relative spelling, as for the symlink guard.
    """
    if dest.is_dir():
        raise VaultRootEscapeError(
            f"refusing to write {display!r}: a directory sits at the destination."
        )


def _ensure_directory(directory: Path, display: str) -> None:
    """Create ``directory`` (and its parents), or refuse when something else sits there.

    ``mkdir(exist_ok=True)`` tolerates only a real directory: a dangling link,
    a regular file, or a link to a file at the path raises ``FileExistsError``.
    That is a refusal in everything but type -- nothing was written, and the
    destination cannot be made -- so it is raised as one here and reaches
    callers through the same translation as the other guards rather than as
    an unhandled error. ``display`` is the vault-relative spelling of the
    directory, as for the other guards.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise VaultRootEscapeError(
            f"refusing to write under {display!r}: something that is not a "
            f"directory sits at that path."
        ) from exc


def _disambiguation_token(canonical_hash: str) -> str:
    """The 8-hex token that names a content-disambiguated retained copy.

    The one normalization boundary between the two spellings a digest travels
    in: callers hold the canonical ``sha256:<hex>`` form :func:`hash_file`
    produces, while the collision-suffix naming rule
    (``imports/<stem>_<token><ext>``) predates it and is bare truncated hex.
    Both bindings derive the suffix here so the rule cannot drift between them.
    """
    return canonical_hash.removeprefix("sha256:")[:8]


@dataclass(frozen=True)
class DiscoveredVault:
    """A vault located by discovery, before its configuration is loaded.

    ``config_path`` is the filesystem locator under the filesystem binding and
    ``None`` for a binding with no filesystem path (the binding fetches the
    config from the store itself). ``vault_id`` is the binding-opaque identity a
    pathless binding loads by: the document-store binding populates it during
    discovery and ``load_config`` resolves the config from it, since there is no
    path to thread. The filesystem binding populates it too (from the directory
    name) for symmetry, though its ``load_config`` resolves from ``config_path``.
    Discovery enumerates cheaply; the caller loads each config under its own
    per-vault failure handling via :meth:`VaultSourceStore.load_config`,
    preserving the lifespans' "skip a malformed vault, keep the rest" behavior.
    """

    config_path: Path | None
    vault_id: str | None = None


class VaultSourceStore(ABC):
    """Port for the vault-source-store binding (CAS-ADR-043).

    One instance serves the whole stack. The layer above it -- vault discovery,
    configuration load and write -- is binding-invariant; the filesystem and
    document-store bindings differ only in where the configuration declaration
    is persisted. SAGE is the sole writer under every binding.
    """

    @abstractmethod
    def discover(self) -> list[DiscoveredVault]:
        """Enumerate the vaults the store holds, without loading their configs."""

    @abstractmethod
    def load_config(self, discovered: DiscoveredVault) -> VaultConfig:
        """Load and parse one discovered vault's configuration declaration."""

    @abstractmethod
    def config_locator(self, vault_id: str) -> Path | None:
        """Return the filesystem path of a vault's config, or ``None``.

        ``None`` for a binding with no filesystem path. The transport lifespans
        thread the returned path into ``initialize_services`` as ``config_path``;
        a ``None`` value is itself a conformant thread (an explicit ``None``).
        """

    @abstractmethod
    def write_config(self, vault_id: str, config_dict: dict) -> None:
        """Persist a vault's configuration declaration (atomically)."""

    @abstractmethod
    def delete_config(self, vault_id: str) -> None:
        """Remove a vault's configuration declaration if present (idempotent)."""

    def close(self) -> None:
        """Release any transport the binding holds. A no-op by default.

        The filesystem binding holds no client, so it inherits this no-op; the
        document-store binding overrides it to close its Graph client. Callers can
        always call ``close()`` on the port without branching on the backend.
        """

    # -- Source-byte half ---------------------------------------------------
    #
    # The store also owns the source files retained from each ingest. These
    # operations are binding-invariant: retention on ingest, and read-back for
    # delivery and integrity audit. ``storage_root`` is the per-vault source
    # root (a filesystem locator under the filesystem binding); ``vault_id``
    # identifies the vault for a binding that addresses sources by vault rather
    # than by path. A binding uses whichever of the two its addressing model
    # needs and ignores the other, mirroring how the dispatch contract treats
    # ``vault_root`` and ``managed_identity``.

    @abstractmethod
    def planned_source_path(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        """The vault-relative path :meth:`retain_source` would retain ``source_path`` at.

        The naming rule alone, with no collision handling and no side effects:
        where this binding *homes* a source, before any question of what is
        already there. Each binding's ``retain_source`` derives its own target
        from this method rather than restating the rule, so the answer given to
        a caller reasoning about placement and the path retention actually picks
        cannot drift apart. Collision disambiguation stays inside
        ``retain_source``: this is the un-disambiguated target, so it names where
        a source lands only when nothing else already sits there.
        """

    @abstractmethod
    def retain_source(
        self,
        vault_id: str,
        storage_root: Path,
        source_path: Path,
        delivered_hash: str | None = None,
    ) -> str:
        """Retain an ingest source on the store; return its vault-relative path.

        A source already inside the store is retained in place and its
        vault-relative path returned unchanged. An external source is copied in
        (under ``imports/`` on the filesystem binding); on a name collision the
        identical-content copy is reused, and differing content is disambiguated
        with a content-hash suffix. UI-layer invisibility markers are stripped
        from the retained copy (CAS-ADR-016).

        ``delivered_hash`` is the canonical ``sha256:<hex>`` digest of
        ``source_path``'s bytes, when the caller already holds one. A caller
        that must hash before retaining -- an ingest resolves content identity
        first, so it always does -- passes it here rather than leaving the
        binding to take a second digest of the same file. The value is
        *trusted*: it decides both the identical-content reuse and the
        disambiguation suffix, so a caller supplying a digest that does not
        describe these bytes gets a copy homed under the wrong name. Optional,
        and defaulting to computing it, so the port stays satisfiable by its
        weakest binding and no caller is obliged to hash first.
        """

    @abstractmethod
    def write_source(
        self, vault_id: str, storage_root: Path, source_path: str, source_file: Path
    ) -> str:
        """Stream ``source_file`` to the vault-relative path the *caller* named.

        Returns the canonical ``sha256:<hex>`` digest of the copy the store
        holds once the write is done. The file is streamed from its path and
        never held whole, on either binding. A binding that stores what it is
        handed digests the bytes as they pass and reports that; a binding whose
        store may rewrite the copy at rest reads the copy back to answer, since
        what it holds cannot then be known from what was sent. Either way the
        caller learns whether the store changed the bytes by comparing the
        returned digest to the one it delivered, with no read of its own. That
        comparison carries its meaning only for a delivered file that is
        quiescent between the caller's digest and this write: the two are
        separate passes over the caller's path, so a file changing underneath
        them reads as a store rewrite.

        The complement of :meth:`retain_source`, and deliberately not a mode of
        it. ``retain_source`` answers "find a home for this source" and owns the
        placement decision, so bytes that differ from what already sits at its
        target read as a name collision and are disambiguated to a second path.
        This method answers "put these bytes here": create-or-replace at exactly
        the given path, no naming rule, no collision handling, no
        disambiguation.

        The operation behind repairing a retained copy that changed outside
        SAGE. Such a copy is detected by the source-file integrity audit but
        cannot be repaired by re-delivering the source, precisely because
        ``retain_source`` would read the difference as a collision and move the
        document rather than restore it (CAS-ADR-043).

        Overwrites unconditionally: the caller establishes that these bytes
        belong at this path before calling. Missing parent directories are
        created. A path that resolves outside the vault's source root is refused.
        """

    @abstractmethod
    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        """Whether a retained source is present on the store."""

    @abstractmethod
    def source_is_symlink(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        """Whether the retained path is itself a link rather than the copy.

        The one question about a retained source that every other read answers
        about its *target*: ``source_exists`` and ``hash_source`` both resolve a
        link, so a path something other than SAGE replaced with a link to a file
        holding the expected bytes reads as an intact copy. The write side
        refuses such a path, so nothing would surface it until a repair was
        attempted; this is what lets a caller see it first.

        Independent of presence, and deliberately so: a link whose target is
        absent is still a link at the path the record names, and refusing to
        write there does not depend on what it points at.

        A binding whose store cannot hold links answers False. That is the true
        answer for such a store rather than an unimplemented one, which is why
        the question belongs on the port instead of behind a capability probe.
        """

    @abstractmethod
    def source_is_out_of_root(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        """Whether the retained path names somewhere outside the vault's source tree.

        The second question the write side asks about a path, and the companion
        of :meth:`source_is_symlink` rather than a restatement of it. That one
        asks whether the recorded path's own final component is a link; this asks
        where the path as a whole lands. The two do not always agree -- a plain
        file under an ancestor pointing outside the tree is not a link, and a
        link resolving back inside the tree does not leave it -- and they call
        for different repairs, which is why they stay separate facts: a link is
        removed, while a path that leaves the tree is re-pointed or the vault
        reconfigured.

        Independent of presence, as linkedness is: ``write_source`` refuses such
        a path whether or not anything resolves behind it, so a caller told only
        that the copy is absent would be sent to a repair the store then
        declines at the very path the record holds.

        Each binding answers for the containment its own ``write_source``
        enforces. A binding with no local tree to resolve against answers from
        the shape of the path alone; that is the whole of the containment
        promise on such a side, not a weaker stand-in for one.
        """

    @abstractmethod
    def source_size(self, vault_id: str, storage_root: Path, source_path: str) -> int:
        """Byte size of a retained source, read cheaply (without loading it)."""

    @abstractmethod
    def read_source(self, vault_id: str, storage_root: Path, source_path: str) -> bytes:
        """Read back a retained source's bytes."""

    @abstractmethod
    def iter_source(self, vault_id: str, storage_root: Path, source_path: str) -> Iterator[bytes]:
        """Yield a retained source's bytes in bounded chunks.

        The streaming counterpart of ``read_source`` for delivery paths that
        must never hold the whole file in memory. Chunks are at most
        ``_SOURCE_CHUNK_BYTES`` long; closing the iterator early releases any
        binding-held resources (open file, live HTTP response).
        """

    @abstractmethod
    def hash_source(self, vault_id: str, storage_root: Path, source_path: str) -> str:
        """SHA-256 of a retained source in canonical ``sha256:<hex>`` form."""

    @abstractmethod
    def delete_source_tree(self, vault_id: str, storage_root: Path | None) -> None:
        """Remove a vault's retained-source tree (idempotent).

        The teardown counterpart of :meth:`retain_source`: removes the whole
        source tree the store holds for one vault, used by the out-of-band
        vault-teardown path. The filesystem binding removes ``storage_root``
        (guarded against escaping the bound vault root); a binding that addresses
        sources by vault id removes by that id and ignores ``storage_root`` (which
        is ``None`` for a binding with no filesystem locator). Idempotent -- an
        already-absent tree is a no-op.
        """


@runtime_checkable
class SupportsSourceDownloadUrl(Protocol):
    """Optional binding capability: mint a short-lived source download URL.

    A richer-binding capability (CAS-ADR-043): a binding whose backing store can
    issue pre-authenticated URLs (the document-store binding) implements this so a
    source can be delivered to a browser directly, without proxying the bytes
    through SAGE. A binding without the capability (the filesystem binding) simply
    does not implement it, and a caller probes with ``isinstance`` before use. It
    is deliberately not part of the ``VaultSourceStore`` port contract, which stays
    satisfiable by its weakest binding.
    """

    def download_url(self, vault_id: str, storage_root: Path, source_path: str) -> str | None:
        """Return a short-lived download URL for a retained source, or ``None``.

        ``None`` when the source is absent from the store. ``storage_root`` and
        ``vault_id`` mirror the other source-byte operations' addressing.
        """


# ---------------------------------------------------------------------------
# UI-layer metadata normalization (CAS-ADR-016)
# ---------------------------------------------------------------------------
#
# Agents often flag their working temp files invisible on macOS (BSD
# UF_HIDDEN chflag, or com.apple.FinderInfo invisible bit). When the
# filesystem binding copies such a file into the vault via shutil.copy2, the
# BSD chflag propagates to the canonical copy, hiding it from Finder. The
# invisible bit encodes source-artifact semantics ("this is scratch"), not
# canonical-artifact semantics -- the vault is the state substrate and its
# files must remain user-auditable.
#
# Empirical behavior on macOS + CPython 3.12/3.14:
# * shutil.copy2 DOES propagate UF_HIDDEN (via os.chflags in copystat).
# * shutil.copy2 does NOT propagate com.apple.FinderInfo xattr on macOS
# (Python stdlib has no xattr API there; _copyxattr is a no-op).
#
# Clearing the xattr is therefore defensive: guards against future Python
# versions that add macOS xattr support, alternative copy mechanisms, or
# filesystem operations that propagate FinderInfo.
#
# macOS lacks a Python stdlib xattr API, so we call libc's getxattr /
# setxattr / removexattr via ctypes. No third-party dependency.


_XATTR_NOFOLLOW = 0x0001
_FINDER_INFO_NAME = b"com.apple.FinderInfo"
_FINDER_INFO_LEN = 32
_FINDER_INVISIBLE_MASK = 0x40  # bit 0x40 in byte 8 of FinderInfo


def _macos_libc() -> ctypes.CDLL | None:
    """Load libc on macOS and declare signatures for xattr functions.

    Returns None on non-macOS platforms so callers can treat absence as
    "nothing to sanitize."
    """
    if sys.platform != "darwin":
        return None
    lib_path = ctypes.util.find_library("c")
    if lib_path is None:
        return None
    libc = ctypes.CDLL(lib_path, use_errno=True)

    # ssize_t getxattr(const char *path, const char *name,
    # void *value, size_t size,
    # u_int32_t position, int options);
    libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t

    # int setxattr(const char *path, const char *name,
    # void *value, size_t size,
    # u_int32_t position, int options);
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int

    # int removexattr(const char *path, const char *name, int options);
    libc.removexattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    libc.removexattr.restype = ctypes.c_int

    return libc


# Cached at module load. None on non-macOS.
_LIBC = _macos_libc()


def _read_finder_info(path: Path) -> bytes | None:
    """Return com.apple.FinderInfo payload, or None if absent / unavailable."""
    if _LIBC is None:
        return None
    buf = (ctypes.c_ubyte * _FINDER_INFO_LEN)()
    rc = _LIBC.getxattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        buf,
        _FINDER_INFO_LEN,
        0,
        _XATTR_NOFOLLOW,
    )
    if rc < 0:
        return None
    return bytes(buf)[:rc]


def _write_finder_info(path: Path, data: bytes) -> bool:
    """Write com.apple.FinderInfo; returns True on success."""
    if _LIBC is None:
        return False
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    rc = _LIBC.setxattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        buf,
        len(data),
        0,
        _XATTR_NOFOLLOW,
    )
    return rc == 0


def _remove_finder_info(path: Path) -> bool:
    """Remove com.apple.FinderInfo; returns True on success or absent."""
    if _LIBC is None:
        return False
    rc = _LIBC.removexattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        _XATTR_NOFOLLOW,
    )
    return rc == 0


def _strip_ui_invisibility(path: Path) -> None:
    """Clear macOS UI-invisibility markers from a file.

    On macOS: clears the BSD UF_HIDDEN chflag and clears bit 0x40 in
    byte 8 of com.apple.FinderInfo (kIsInvisible). Preserves all other
    bytes of the xattr (type/creator codes, color labels, stationery
    flag, etc.).

    On non-macOS platforms: no-op.

    Errors are swallowed: UI-layer sanitization is best-effort and must
    not fail an ingest. Logged at debug level for diagnosis.
    """
    if sys.platform != "darwin":
        return

    # 1. BSD UF_HIDDEN chflag.
    try:
        st = os.lstat(str(path))
        flags = getattr(st, "st_flags", 0)
        if flags & stat.UF_HIDDEN:
            os.chflags(str(path), flags & ~stat.UF_HIDDEN)
    except (OSError, AttributeError) as exc:
        logger.debug("UF_HIDDEN sanitization failed for %s: %s", path, exc)

    # 2. com.apple.FinderInfo invisible bit.
    try:
        info = _read_finder_info(path)
        if info is None or len(info) < 9:
            return
        if not (info[8] & _FINDER_INVISIBLE_MASK):
            return  # bit not set; nothing to do
        new_info = bytearray(info)
        new_info[8] &= ~_FINDER_INVISIBLE_MASK
        # Pad / truncate to canonical 32 bytes for Finder compatibility.
        if len(new_info) < _FINDER_INFO_LEN:
            new_info.extend(b"\x00" * (_FINDER_INFO_LEN - len(new_info)))
        elif len(new_info) > _FINDER_INFO_LEN:
            new_info = new_info[:_FINDER_INFO_LEN]
        # If every byte is zero after clearing, remove the xattr entirely.
        if all(b == 0 for b in new_info):
            _remove_finder_info(path)
        else:
            _write_finder_info(path, bytes(new_info))
    except Exception as exc:  # noqa: BLE001 -- best-effort sanitization
        logger.debug("FinderInfo sanitization failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Resilient recursive removal (vault teardown)
# ---------------------------------------------------------------------------
#
# A vault teardown removes whole directory trees (storage_root, brain_root, and
# the enclosing vault dir). A directory can be repopulated between shutil.rmtree's
# scan and its final os.rmdir -- macOS re-creates a .DS_Store inside a directory
# as it is being removed, and a live server's per-vault timing.log writer keeps
# writing into a tree the standalone teardown CLI (which cannot evict a foreign
# server's registry) is removing -- leaving the rmdir raising ENOTEMPTY. The
# removal is bounded-retried, stripping macOS UI-artifact files (the .DS_Store
# companion to _strip_ui_invisibility's invisibility markers, CAS-ADR-016) before
# each retry.

_MACOS_UI_ARTIFACT_NAME = ".DS_Store"
_RMTREE_MAX_ATTEMPTS = 5
_RMTREE_RETRY_BACKOFF_SECONDS = 0.1


def _strip_macos_ui_artifacts(root: Path) -> None:
    """Delete macOS Finder scratch files (``.DS_Store``) from a tree.

    Finder re-creates ``.DS_Store`` inside a directory even as it is being removed,
    which can leave ``shutil.rmtree``'s final ``os.rmdir`` seeing a non-empty
    directory. Removing these Finder-owned files clears that. Best-effort, mirroring
    :func:`_strip_ui_invisibility` (CAS-ADR-016): a file that reappears after this
    pass is caught by the caller's bounded retry. Not platform-gated -- a file named
    ``.DS_Store`` under a vault tree is Finder scratch on any host, and the tree is
    being deleted regardless.
    """
    if not root.exists():
        return
    for artifact in root.rglob(_MACOS_UI_ARTIFACT_NAME):
        try:
            artifact.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("could not strip %s: %s", artifact, exc)


def remove_tree_tolerating_concurrent_writer(path: Path) -> None:
    """``shutil.rmtree`` that tolerates a concurrent writer repopulating a directory.

    A directory can gain a file between ``rmtree``'s scan and its final ``os.rmdir``
    -- macOS re-creating a ``.DS_Store``, or a live server's per-vault ``timing.log``
    writer under ``brain_root`` that the standalone teardown CLI cannot evict --
    leaving the ``rmdir`` raising ``ENOTEMPTY``. Retries the removal a bounded number
    of times, stripping macOS UI-artifact files (CAS-ADR-016) before each retry; a
    non-transient error, or a persistent one on the final attempt, propagates.
    Idempotent: an already-absent ``path`` is a no-op.
    """
    for attempt in range(_RMTREE_MAX_ATTEMPTS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return  # already gone -- keep the teardown idempotent
        except OSError as exc:
            last_attempt = attempt == _RMTREE_MAX_ATTEMPTS - 1
            if exc.errno != errno.ENOTEMPTY or last_attempt:
                raise
            # A concurrent writer repopulated the directory. Strip known macOS
            # UI-artifact files and retry.
            _strip_macos_ui_artifacts(path)
            time.sleep(_RMTREE_RETRY_BACKOFF_SECONDS)


class FilesystemVaultSourceStore(VaultSourceStore):
    """The filesystem binding: the local vault tree under a single vault root.

    Reproduces today's behavior behind the port, delegating to the existing
    vault-config helpers: discovery scans ``{vault_root}/<vault_id>/`` for
    ``vault_config.yaml`` and each vault's config is read, written, and removed
    at ``{vault_root}/<vault_id>/vault_config.yaml``.
    """

    def __init__(self, vault_root: Path) -> None:
        self._vault_root = vault_root

    def discover(self) -> list[DiscoveredVault]:
        from sage.vault_discovery import discover_vault_configs

        return [
            DiscoveredVault(config_path=p, vault_id=p.parent.name)
            for p in discover_vault_configs(self._vault_root)
        ]

    def load_config(self, discovered: DiscoveredVault) -> VaultConfig:
        from sage.config import load_vault_config

        if discovered.config_path is None:
            raise ValueError(
                "the filesystem vault-source binding requires a config_path on "
                "the discovered vault; got None."
            )
        return load_vault_config(discovered.config_path)

    def config_locator(self, vault_id: str) -> Path:
        return self._vault_root / vault_id / "vault_config.yaml"

    def write_config(self, vault_id: str, config_dict: dict) -> None:
        from sage.vault_management import _write_config_yaml

        _write_config_yaml(self.config_locator(vault_id), config_dict)

    def delete_config(self, vault_id: str) -> None:
        self.config_locator(vault_id).unlink(missing_ok=True)

    @staticmethod
    def _internal_relative(storage_root: Path, source_path: Path) -> str | None:
        """The source's vault-relative path when it already lives under the vault
        root, else ``None`` for an external file."""
        try:
            return str(source_path.relative_to(storage_root))
        except ValueError:
            return None

    def planned_source_path(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        # An internal source is homed where it already sits; anything else lands
        # in ``imports/`` under its own name.
        internal = self._internal_relative(storage_root, source_path)
        return internal if internal is not None else f"imports/{source_path.name}"

    def retain_source(
        self,
        vault_id: str,
        storage_root: Path,
        source_path: Path,
        delivered_hash: str | None = None,
    ) -> str:
        # A source already under the vault root is internal: return its
        # vault-relative path with no copy.
        internal = self._internal_relative(storage_root, source_path)
        if internal is not None:
            return internal

        imports_dir = storage_root / "imports"
        _ensure_directory(imports_dir, "imports")

        # Derived from the naming rule rather than restating it: this path and
        # the one ``planned_source_path`` reports must not be able to drift
        # apart, since anything reasoning about placement without retaining
        # reads the latter.
        dest = storage_root / self.planned_source_path(vault_id, storage_root, source_path)
        reuse = False
        if dest.exists():
            # A directory here is not a collision the branch below can
            # disambiguate around: hashing it fails before any guard has run,
            # so it is refused ahead of the comparison rather than at the
            # settled-destination guards, which it would never reach.
            _assert_not_directory(dest, str(dest.relative_to(storage_root)))
            # The caller's digest when it has one, so the delivered bytes are
            # hashed once across the whole ingest rather than once here and once
            # by whoever had to establish content identity first.
            content_hash = delivered_hash or hash_file(source_path)
            existing_hash = hash_file(dest)
            if content_hash == existing_hash:
                # Identical content already imported: this path is the answer,
                # with no copy to make.
                reuse = True
            else:
                # Different content: disambiguate with the 8-char content hash.
                token = _disambiguation_token(content_hash)
                dest = imports_dir / f"{source_path.stem}_{token}{source_path.suffix}"

        # The destination is settled, so it is guarded once. Both outcomes need
        # the same assurance about the same path and differ only in what they
        # then do with it: the reuse below returns it as the record's
        # ``source_path``, the copy lands bytes at it. Deciding first and
        # guarding after is what keeps that true, since a guard placed on a
        # candidate destination is only ever correct for the branch that happens
        # to keep it. The one exit that does not arrive here is the internal
        # short-circuit above, which returns a path already inside the tree
        # without choosing one. The directory check alone appears twice: the
        # planned path had to be examined before the collision comparison
        # hashed it, which is earlier than any settled destination exists.
        #
        # Every guard names the destination by its vault-relative spelling. A
        # refusal travels to the caller verbatim, and that spelling is the one
        # it can relate to what it sent; an absolute server path would only
        # disclose the host's layout.
        vault_relative = str(dest.relative_to(storage_root))
        # The disambiguated name can be a directory as readily as the planned
        # one, and ``copy2`` into a directory raises nothing -- it lands the
        # bytes *under* it while the returned path would name the directory.
        _assert_not_directory(dest, vault_relative)
        # A link at the destination would otherwise redirect a copy onto its
        # target, or become a record's ``source_path`` -- every read following
        # it wherever its owner points, and a later repair refusing the very
        # path the record names.
        _assert_not_symlinked(dest, vault_relative)
        # Containment resolves the whole path, so a symlinked *ancestor* --
        # ``imports/`` itself, say -- cannot land the copy outside the tree while
        # the returned vault-relative path claims it is inside. Asserted rather
        # than substituted: the copy goes to the path as named, and the returned
        # vault-relative form is computed against ``storage_root`` as given, so a
        # storage root that is itself reached through a link keeps working.
        resolve_and_assert_within_root(dest, storage_root, display=vault_relative)

        if not reuse:
            shutil.copy2(source_path, dest)
            # Strip UI-layer invisibility markers that shutil.copy2 may have
            # propagated from an agent's temp source (CAS-ADR-016).
            _strip_ui_invisibility(dest)
        return vault_relative

    def write_source(
        self, vault_id: str, storage_root: Path, source_path: str, source_file: Path
    ) -> str:
        _assert_plain_vault_relative(source_path)
        dest = storage_root / source_path
        # This operation's precondition is that something other than SAGE wrote
        # to the store, so a planted link -- or a directory -- is squarely in
        # scope here. Each guard names the path as the caller spelled it.
        _assert_not_symlinked(dest, source_path)
        _assert_not_directory(dest, source_path)
        # Containment is checked against the *source root*, not the bound vault
        # root: the caller names a vault-relative path, and the guarantee owed is
        # that it stays inside the tree this vault's sources live in.
        dest = resolve_and_assert_within_root(dest, storage_root, display=source_path)
        # A delivered file that *is* the retained copy -- its own path, or a
        # link to it -- is refused before anything is opened. The streaming
        # write below truncates the destination before it has read the source,
        # and on a shared inode that would empty the copy and report the empty
        # digest as what was written; there are no bytes anywhere else to
        # recover it from. Compared by inode, not by spelling, so a link
        # outside the tree pointing at the copy is caught with it.
        if dest.exists() and source_file.exists() and os.path.samefile(source_file, dest):
            raise VaultRootEscapeError(
                f"refusing to write {source_path!r}: the delivered file is the "
                "retained copy itself."
            )
        _ensure_directory(dest.parent, str(PurePosixPath(source_path).parent))
        # One pass: the delivered file is streamed into place and digested as
        # it goes by, so it is never held whole and the destination is never
        # read back. This binding stores what it is handed, so the digest of
        # the bytes in flight is the digest of the copy at rest. The source is
        # opened before the destination is truncated, so a missing source
        # leaves the existing copy as it was -- and a source that is the
        # destination never reaches this point at all.
        digest = hashlib.sha256()
        with source_file.open("rb") as incoming, dest.open("wb") as outgoing:
            for chunk in iter(lambda: incoming.read(_SOURCE_CHUNK_BYTES), b""):
                digest.update(chunk)
                outgoing.write(chunk)
        # Same CAS-ADR-016 sanitization ``retain_source`` applies: a restored
        # copy must be no more UI-visible-or-hidden than a freshly retained one.
        _strip_ui_invisibility(dest)
        return f"sha256:{digest.hexdigest()}"

    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        return (storage_root / source_path).exists()

    def source_is_symlink(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        # ``is_symlink`` is an lstat: it describes the named path itself, where
        # every other read here resolves through it. That is the whole point --
        # it is the only way this binding can report the state its own write
        # guards refuse to create.
        return (storage_root / source_path).is_symlink()

    def source_is_out_of_root(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        # Asked by running the guards themselves rather than by restating the
        # rule they encode, so the answer a caller reads and the verdict a write
        # would receive cannot drift apart -- the same reason ``retain_source``
        # derives its destination from ``planned_source_path``. Both of
        # ``write_source``'s containment guards are run: the shape check refuses
        # a path that never had a chance of being vault-relative, and the resolve
        # catches one whose components lead out of the tree. The symlink and
        # directory guards are deliberately not run here; they refuse for
        # reasons of their own, and ``source_is_symlink`` answers the first.
        try:
            _assert_plain_vault_relative(source_path)
            resolve_and_assert_within_root(storage_root / source_path, storage_root)
        except VaultRootEscapeError:
            return True
        return False

    def source_size(self, vault_id: str, storage_root: Path, source_path: str) -> int:
        return (storage_root / source_path).stat().st_size

    def read_source(self, vault_id: str, storage_root: Path, source_path: str) -> bytes:
        return (storage_root / source_path).read_bytes()

    def iter_source(self, vault_id: str, storage_root: Path, source_path: str) -> Iterator[bytes]:
        with (storage_root / source_path).open("rb") as f:
            yield from iter(lambda: f.read(_SOURCE_CHUNK_BYTES), b"")

    def hash_source(self, vault_id: str, storage_root: Path, source_path: str) -> str:
        return hash_file(storage_root / source_path)

    def delete_source_tree(self, vault_id: str, storage_root: Path | None) -> None:
        # Guard the (config-authorable) storage_root against escaping the bound
        # vault root before the recursive delete, then remove it. Idempotent: an
        # already-absent tree resolves fine and the rmtree is skipped. The
        # teardown core only reaches this binding with a real storage_root; a None
        # (no filesystem locator) is a no-op.
        if storage_root is None:
            return
        resolved = resolve_and_assert_within_root(storage_root, self._vault_root)
        if resolved.exists():
            remove_tree_tolerating_concurrent_writer(resolved)


class DocumentStoreVaultSourceStore(VaultSourceStore):
    """The cloud document-store binding: a SharePoint library over Graph (CAS-ADR-043).

    Persists each vault's configuration declaration to a Microsoft 365 SharePoint
    document library under the workload's managed identity, so a cloud vault's
    config survives the stateless compute's restart. It has no filesystem path:
    ``config_locator`` returns ``None``, discovery carries each vault's id rather
    than a path, and ``load_config`` resolves the config from that id.

    The Graph adapter (the Azure-SDK import and the raw REST calls) lives in
    ``sage.vault_source_document_store`` and is reached only through this binding,
    keeping this port module free of any Azure import. The client is injected for
    tests; in the cloud profile the factory builds it eagerly so the managed
    identity resolves at startup, and otherwise it is built lazily on first use.
    """

    def __init__(
        self,
        config: StackDocumentStoreConfig,
        *,
        client: object | None = None,
        managed_identity: bool = True,
    ) -> None:
        self._config = config
        self._client = client
        self._managed_identity = managed_identity

    def _get_client(self) -> object:
        if self._client is None:
            from sage.vault_source_document_store import build_sharepoint_graph_client

            self._client = build_sharepoint_graph_client(
                self._config, managed_identity=self._managed_identity
            )
        return self._client

    def close(self) -> None:
        """Close the Graph client if one was built (eagerly or on first use).

        A no-op when the client was never constructed -- a lazily-bound store that
        served no request holds no transport to release.
        """
        client = self._client
        if client is not None and hasattr(client, "close"):
            client.close()

    def discover(self) -> list[DiscoveredVault]:
        client = self._get_client()
        return [
            DiscoveredVault(config_path=None, vault_id=vault_id)
            for vault_id in client.list_vault_ids()  # type: ignore[attr-defined]
        ]

    def load_config(self, discovered: DiscoveredVault) -> VaultConfig:
        if discovered.vault_id is None:
            raise ValueError(
                "the document-store vault-source binding requires a vault_id on the "
                "discovered vault; got None."
            )
        client = self._get_client()
        data = client.read_config_bytes(discovered.vault_id)  # type: ignore[attr-defined]
        if data is None:
            raise FileNotFoundError(
                f"no vault configuration declaration for vault {discovered.vault_id!r} "
                "in the document store."
            )
        import yaml

        raw = yaml.safe_load(data)
        # Warn on retired sections before validation, matching the filesystem
        # binding's load path: VaultConfig drops unknown keys, so the raw
        # mapping is the only place the stale section is still visible
        # (CAS-ADR-046).
        warn_on_retired_sections(raw)
        # A stored configuration is a fact, not a request: lifecycle-shape
        # violations load with a warning instead of rejecting, matching
        # load_vault_config — a rejected declaration drops its vault from
        # the registry, unreachable by the surfaces that could repair it.
        return VaultConfig.model_validate(raw, context={"lifecycle_validation": "warn"})

    def config_locator(self, vault_id: str) -> Path | None:
        return None

    def write_config(self, vault_id: str, config_dict: dict) -> None:
        import yaml

        # Serialize with the same yaml options as the filesystem binding so the
        # stored declaration round-trips identically. Validation is the caller's
        # responsibility under both bindings (the weakest-binding rule); the
        # service layer validates before persisting.
        text = yaml.safe_dump(config_dict, default_flow_style=False, sort_keys=False)
        data = text.encode("utf-8")
        client = self._get_client()
        client.write_config_bytes(vault_id, data)  # type: ignore[attr-defined]

    def delete_config(self, vault_id: str) -> None:
        client = self._get_client()
        client.delete_config(vault_id)  # type: ignore[attr-defined]

    def planned_source_path(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        # Every source is re-homed under ``imports/``: this binding has no
        # filesystem locator, so there is no "already inside the tree" case to
        # preserve. ``storage_root`` is unused, as it is for every other source
        # operation here.
        return f"imports/{source_path.name}"

    def retain_source(
        self,
        vault_id: str,
        storage_root: Path,
        source_path: Path,
        delivered_hash: str | None = None,
    ) -> str:
        # Every retain uploads to the document store: under this binding the
        # local tree is ephemeral, so the filesystem binding's "already-internal,
        # no copy" optimization is meaningless here. ``storage_root`` is unused --
        # the source is addressed by vault id and vault-relative path, mirroring
        # how the config surface ignores the filesystem locator. UI-invisibility
        # stripping is a macOS-filesystem concern; a Graph upload of raw bytes
        # carries no xattr/chflag, so there is nothing to strip.
        #
        # The caller's digest when it has one, else a streamed one: the file
        # is never loaded here, since the upload streams it from its path.
        content_hash = delivered_hash or hash_file(source_path)
        client = self._get_client()
        rel = self.planned_source_path(vault_id, storage_root, source_path)
        if client.source_item(vault_id, rel) is not None:  # type: ignore[attr-defined]
            # Hashed at the store, streamed, rather than pulled down and hashed
            # here: the comparison needs a digest, and materializing the whole
            # stored copy to obtain one costs a full network round-trip of a file
            # this method is about to overwrite or walk past either way.
            existing_hash = client.hash_source_bytes(vault_id, rel)  # type: ignore[attr-defined]
            if existing_hash == content_hash:
                # Identical content already retained -- reuse the existing path.
                return rel
            # Different content under the same name: disambiguate with the hash.
            #
            # Note the limit of the comparison above: it weighs the *incoming*
            # bytes against the bytes the store holds. For a format the store
            # rewrites at rest those can never be equal, so re-delivering such a
            # source reaches this line and lands a second copy. The binding
            # cannot do better -- it holds no record of what the stored copy was
            # made from -- so the reuse decision for those formats is taken by
            # the caller, which does hold that record, before it ever calls this
            # method (see ``planned_source_path``).
            token = _disambiguation_token(content_hash)
            rel = f"imports/{source_path.stem}_{token}{source_path.suffix}"
        client.upload_source(vault_id, rel, source_path)  # type: ignore[attr-defined]
        return rel

    def write_source(
        self, vault_id: str, storage_root: Path, source_path: str, source_file: Path
    ) -> str:
        # A streamed upload to the named key. The store's own create-or-replace
        # on a path is the whole primitive here; ``storage_root`` is unused, as
        # it is for every other source operation under this binding.
        #
        # The shape check is not redundant with the filesystem binding's: there
        # is no local tree here for a resolver to catch an escape against, and
        # the client's URL builder joins path segments verbatim -- so without it
        # the port's containment promise would have no implementation at all on
        # this side.
        _assert_plain_vault_relative(source_path)
        client = self._get_client()
        client.upload_source(vault_id, source_path, source_file)  # type: ignore[attr-defined]
        # The as-stored digest is a streamed read of the copy, and this is the
        # one binding where that read still belongs: the store rewrites some
        # formats at rest and reports no SHA-256 of its own, so what it now
        # holds cannot be known from what was sent.
        return client.hash_source_bytes(vault_id, source_path)  # type: ignore[attr-defined]

    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        return self._get_client().source_item(vault_id, source_path) is not None  # type: ignore[attr-defined]

    def source_is_symlink(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        # An item in a document library is a stored object, and nothing in this
        # binding's write path can produce a link at the key it writes, so no
        # retained source's final component is ever one. Scoped to that leaf,
        # which is all this method claims: the store does have a shortcut
        # concept, and a shortcut standing in for a *folder* on the way to a key
        # is not answered here any more than a symlinked ancestor is on the
        # filesystem binding. Answered without a lookup because the answer is a
        # property of the store, not of any item: a round-trip could only return
        # the same constant more expensively, and an absent item would then be
        # indistinguishable from a present one.
        return False

    def source_is_out_of_root(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        # The shape of the path is the whole containment question on this side.
        # There is no local tree for a resolver to walk, and the client's URL
        # builder joins segments verbatim, so the shape check is what this
        # binding's ``write_source`` refuses on -- and running it here is what
        # keeps the answer and the refusal one fact rather than two.
        #
        # Scoped to the key, as ``source_is_symlink`` is scoped to the leaf: the
        # store has a shortcut concept, and a folder shortcut standing in for a
        # segment on the way to a key is not answered here, any more than a
        # linked ancestor is answered by that method. Answered without a lookup,
        # since the path alone decides it -- a round-trip could only return the
        # same answer more expensively.
        try:
            _assert_plain_vault_relative(source_path)
        except VaultRootEscapeError:
            return True
        return False

    def source_size(self, vault_id: str, storage_root: Path, source_path: str) -> int:
        item = self._get_client().source_item(vault_id, source_path)  # type: ignore[attr-defined]
        if item is None:
            raise FileNotFoundError(
                f"no retained source {source_path!r} for vault {vault_id!r} in the document store."
            )
        return int(item["size"])

    def read_source(self, vault_id: str, storage_root: Path, source_path: str) -> bytes:
        return self._get_client().read_source_bytes(vault_id, source_path)  # type: ignore[attr-defined]

    def iter_source(self, vault_id: str, storage_root: Path, source_path: str) -> Iterator[bytes]:
        yield from self._get_client().stream_source_bytes(vault_id, source_path)  # type: ignore[attr-defined]

    def hash_source(self, vault_id: str, storage_root: Path, source_path: str) -> str:
        return self._get_client().hash_source_bytes(vault_id, source_path)  # type: ignore[attr-defined]

    def delete_source_tree(self, vault_id: str, storage_root: Path | None) -> None:
        # The document store addresses the vault's tree by id, not by a filesystem
        # path: a single Graph folder delete removes the whole vault folder (its
        # config and every retained source) server-side. ``storage_root`` is unused
        # (None under this binding, which has no filesystem locator). Idempotent --
        # a missing folder is tolerated by the client.
        self._get_client().delete_tree(vault_id)  # type: ignore[attr-defined]

    def download_url(self, vault_id: str, storage_root: Path, source_path: str) -> str | None:
        """Return a short-lived pre-authenticated download URL for a retained source.

        Delegates to the Graph adapter, which reads the driveItem's
        ``@microsoft.graph.downloadUrl`` -- a pre-authenticated, time-limited URL
        that needs no bearer token -- so the browser fetches the bytes directly
        from SharePoint. Returns ``None`` when the source is absent from the store.
        ``storage_root`` is unused (the source is addressed by vault id and
        vault-relative path), mirroring the other source operations.
        """
        return self._get_client().source_download_url(vault_id, source_path)  # type: ignore[attr-defined]


def build_stack_vault_source_store(
    stack_config: SageCoreConfig,
    *,
    vault_root: Path | None = None,
    managed_identity: bool = False,
) -> VaultSourceStore:
    """Construct the stack-wide vault-source store (CAS-ADR-043).

    Dispatch contract (mirrors :func:`sage.storage_binding.build_stack_storage_provisioner`):
      1. ``SAGE_TEST_VAULT_SOURCE_BACKEND`` set -> that backend (env override,
         topmost so the test suite can pin the filesystem binding process-wide
         while a committed config selects the document store)
      2. ``stack.vault_source_backend == "filesystem"`` -> the filesystem
         binding under ``vault_root`` (defaulting to ``default_vault_root()``)
      3. ``stack.vault_source_backend == "document_store"`` -> the document-store
         binding (stubbed in this slice)

    An unrecognized env value fails loud: a typo'd override silently falling
    through to the configured backend could persist a vault's config to the
    wrong store.

    ``vault_root`` is the filesystem binding's root. The transport lifespans
    pass the root they resolved from ``--vault-root`` / ``SAGE_VAULT_ROOT`` / the
    default; callers that pass nothing get ``default_vault_root()``. The
    document-store binding ignores it. ``managed_identity`` is the cloud
    profile's selector: for the document-store binding it builds the Graph client
    eagerly so the managed identity (and its Azure SDK) resolves at startup,
    mirroring the storage binding's managed-identity path; it is a no-op for the
    filesystem binding.
    """
    backend = os.environ.get(VAULT_SOURCE_BACKEND_ENV_VAR) or stack_config.vault_source_backend
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown vault-source backend {backend!r} (from "
            f"{VAULT_SOURCE_BACKEND_ENV_VAR}); expected one of {_VALID_BACKENDS}."
        )
    if backend == "document_store":
        client = None
        if managed_identity:
            from sage.vault_source_document_store import build_sharepoint_graph_client

            client = build_sharepoint_graph_client(
                stack_config.document_store, managed_identity=True
            )
        return DocumentStoreVaultSourceStore(
            stack_config.document_store,
            client=client,
            managed_identity=managed_identity,
        )
    from sage.vault_management import default_vault_root

    root = vault_root if vault_root is not None else default_vault_root()
    return FilesystemVaultSourceStore(root)
