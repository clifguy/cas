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

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from sage.config import SageCoreConfig, VaultConfig

# Environment override for the vault-source-backend dispatch, consulted before
# the stack config's ``vault_source_backend`` key. Mirrors
# ``SAGE_TEST_STORAGE_BACKEND`` so the test suite can pin the filesystem binding
# process-wide while a committed cloud config selects the document store.
VAULT_SOURCE_BACKEND_ENV_VAR = "SAGE_TEST_VAULT_SOURCE_BACKEND"

_VALID_BACKENDS = ("filesystem", "document_store")

_FOLLOW_UP = (
    "the document-store vault-source binding (a tenant-native SharePoint "
    "document library over Microsoft Graph) is not yet implemented; CAS-ADR-043 "
    "establishes the port and the filesystem binding, and the concrete "
    "document-store adapter lands in a follow-up. Select the filesystem binding "
    "(vault_source_backend: filesystem) until then."
)


@dataclass(frozen=True)
class DiscoveredVault:
    """A vault located by discovery, before its configuration is loaded.

    ``config_path`` is the filesystem locator under the filesystem binding and
    ``None`` for a binding with no filesystem path (the binding fetches the
    config from the store itself). Discovery enumerates cheaply; the caller
    loads each config under its own per-vault failure handling via
    :meth:`VaultSourceStore.load_config`, preserving the lifespans' "skip a
    malformed vault, keep the rest" behavior.
    """

    config_path: Path | None


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

        return [DiscoveredVault(config_path=p) for p in discover_vault_configs(self._vault_root)]

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


class DocumentStoreVaultSourceStore(VaultSourceStore):
    """Stub for the cloud tenant document-store binding (CAS-ADR-043).

    The port and the filesystem binding land in this slice; the concrete
    SharePoint/Graph adapter -- managed-identity auth, atomic-write emulation
    over the non-POSIX API, throttle/retry, enumeration -- is a follow-up. Every
    method fails loud rather than silently no-opping, so a deployment that
    selects this binding before the adapter exists is told exactly why. The
    binding is registered for the cloud profile so the seam roster stays
    complete; it is reached only when ``vault_source_backend`` is explicitly set
    to ``document_store``.
    """

    def discover(self) -> list[DiscoveredVault]:
        raise NotImplementedError(_FOLLOW_UP)

    def load_config(self, discovered: DiscoveredVault) -> VaultConfig:
        raise NotImplementedError(_FOLLOW_UP)

    def config_locator(self, vault_id: str) -> Path | None:
        raise NotImplementedError(_FOLLOW_UP)

    def write_config(self, vault_id: str, config_dict: dict) -> None:
        raise NotImplementedError(_FOLLOW_UP)

    def delete_config(self, vault_id: str) -> None:
        raise NotImplementedError(_FOLLOW_UP)


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
    profile's selector, reserved for the document-store binding's managed-identity
    auth; it is a no-op for the filesystem binding.
    """
    backend = os.environ.get(VAULT_SOURCE_BACKEND_ENV_VAR) or stack_config.vault_source_backend
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown vault-source backend {backend!r} (from "
            f"{VAULT_SOURCE_BACKEND_ENV_VAR}); expected one of {_VALID_BACKENDS}."
        )
    if backend == "document_store":
        return DocumentStoreVaultSourceStore()
    from sage.vault_management import default_vault_root

    root = vault_root if vault_root is not None else default_vault_root()
    return FilesystemVaultSourceStore(root)
