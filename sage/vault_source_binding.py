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
from pathlib import Path
from typing import Protocol, runtime_checkable

from sage.config import SageCoreConfig, StackDocumentStoreConfig, VaultConfig

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
    """A filesystem teardown target resolved outside the process-bound vault root.

    Raised by :func:`resolve_and_assert_within_root` when a path a destructive
    operation is about to remove does not realpath-resolve to a strict descendant
    of the bound vault root. A ``ValueError`` subclass so an existing
    ``except ValueError`` handler routes it like any other shape failure.
    """


def resolve_and_assert_within_root(path: Path, vault_root: Path) -> Path:
    """Realpath-resolve ``path`` and require it to be a strict descendant of ``vault_root``.

    The safety primitive behind every vault-teardown ``rmtree``: a vault's
    ``storage_root`` / ``brain_root`` / config path come from its
    (operator- or typo-authorable) configuration, so before any recursive delete
    each is resolved through symlinks and asserted to live *strictly under* the
    bound vault root (CAS-ADR-043's ``get_vault_root``). A path that resolves
    outside the root -- or to the root itself, since removing the root would
    destroy every vault -- raises :class:`VaultRootEscapeError` and nothing is
    deleted. Returns the resolved path on success so the caller removes the
    canonical (symlink-free) target. ``path`` need not exist: an already-absent
    target still resolves, keeping the teardown idempotent.
    """
    resolved = path.expanduser().resolve()
    root = vault_root.expanduser().resolve()
    if resolved == root or root not in resolved.parents:
        raise VaultRootEscapeError(
            f"refusing to operate on {path} (resolved {resolved}): it is not a "
            f"strict descendant of the bound vault root {root}."
        )
    return resolved


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
    def retain_source(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        """Retain an ingest source on the store; return its vault-relative path.

        A source already inside the store is retained in place and its
        vault-relative path returned unchanged. An external source is copied in
        (under ``imports/`` on the filesystem binding); on a name collision the
        identical-content copy is reused, and differing content is disambiguated
        with a content-hash suffix. UI-layer invisibility markers are stripped
        from the retained copy (CAS-ADR-016).
        """

    @abstractmethod
    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        """Whether a retained source is present on the store."""

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
    def delete_source_tree(self, vault_id: str, storage_root: Path) -> None:
        """Remove a vault's retained-source tree (idempotent).

        The teardown counterpart of :meth:`retain_source`: removes the whole
        source tree the store holds for one vault, used by the out-of-band
        vault-teardown path. The filesystem binding removes ``storage_root``
        (guarded against escaping the bound vault root); a binding that addresses
        sources by vault id removes by that id. Idempotent -- an already-absent
        tree is a no-op.
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

    def retain_source(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        # A source already under the vault root is internal: return its
        # vault-relative path with no copy.
        try:
            return str(source_path.relative_to(storage_root))
        except ValueError:
            pass  # external file -- fall through to import

        imports_dir = storage_root / "imports"
        imports_dir.mkdir(parents=True, exist_ok=True)

        dest = imports_dir / source_path.name
        if dest.exists():
            content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()[:8]
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()[:8]
            if content_hash == existing_hash:
                # Identical content already imported -- reuse existing path.
                return str(dest.relative_to(storage_root))
            # Different content: disambiguate with the 8-char content hash.
            dest = imports_dir / f"{source_path.stem}_{content_hash}{source_path.suffix}"

        shutil.copy2(source_path, dest)
        # Strip UI-layer invisibility markers that shutil.copy2 may have
        # propagated from an agent's temp source (CAS-ADR-016).
        _strip_ui_invisibility(dest)
        return str(dest.relative_to(storage_root))

    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        return (storage_root / source_path).exists()

    def source_size(self, vault_id: str, storage_root: Path, source_path: str) -> int:
        return (storage_root / source_path).stat().st_size

    def read_source(self, vault_id: str, storage_root: Path, source_path: str) -> bytes:
        return (storage_root / source_path).read_bytes()

    def iter_source(self, vault_id: str, storage_root: Path, source_path: str) -> Iterator[bytes]:
        with (storage_root / source_path).open("rb") as f:
            yield from iter(lambda: f.read(_SOURCE_CHUNK_BYTES), b"")

    def hash_source(self, vault_id: str, storage_root: Path, source_path: str) -> str:
        digest = hashlib.sha256()
        with (storage_root / source_path).open("rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def delete_source_tree(self, vault_id: str, storage_root: Path) -> None:
        # Guard the (config-authorable) storage_root against escaping the bound
        # vault root before the recursive delete, then remove it. Idempotent: an
        # already-absent tree resolves fine and the rmtree is skipped.
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

        return VaultConfig.model_validate(yaml.safe_load(data))

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

    def retain_source(self, vault_id: str, storage_root: Path, source_path: Path) -> str:
        # Every retain uploads to the document store: under this binding the
        # local tree is ephemeral, so the filesystem binding's "already-internal,
        # no copy" optimization is meaningless here. ``storage_root`` is unused --
        # the source is addressed by vault id and vault-relative path, mirroring
        # how the config surface ignores the filesystem locator. UI-invisibility
        # stripping is a macOS-filesystem concern; a Graph upload of raw bytes
        # carries no xattr/chflag, so there is nothing to strip.
        data = source_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()[:8]
        client = self._get_client()
        rel = f"imports/{source_path.name}"
        if client.source_item(vault_id, rel) is not None:  # type: ignore[attr-defined]
            existing_hash = hashlib.sha256(
                client.read_source_bytes(vault_id, rel)  # type: ignore[attr-defined]
            ).hexdigest()[:8]
            if existing_hash == content_hash:
                # Identical content already retained -- reuse the existing path.
                return rel
            # Different content under the same name: disambiguate with the hash.
            rel = f"imports/{source_path.stem}_{content_hash}{source_path.suffix}"
        client.upload_source(vault_id, rel, data)  # type: ignore[attr-defined]
        return rel

    def source_exists(self, vault_id: str, storage_root: Path, source_path: str) -> bool:
        return self._get_client().source_item(vault_id, source_path) is not None  # type: ignore[attr-defined]

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

    def delete_source_tree(self, vault_id: str, storage_root: Path) -> None:
        # Deferred to the cloud document-store teardown slice (the tenant-native
        # source-tree delete over Graph). A concrete method so the ABC still
        # instantiates; the local-profile teardown never reaches this binding.
        raise NotImplementedError(
            "delete_source_tree is not implemented for the document-store vault-source binding yet."
        )

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
