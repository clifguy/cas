"""Tests for the vault-source-store binding of the deployment profiles (CAS-ADR-043).

The binding is a store per backend: the filesystem store reproduces today's
on-disk vault tree (discover/load/write/delete the ``vault_config.yaml``
declaration under the vault root), and the document-store binding is the stub
for the tenant-native cloud adapter that lands in a follow-up.
``build_stack_vault_source_store`` is the dispatch between them: the
``SAGE_TEST_VAULT_SOURCE_BACKEND`` environment override first, then the stack
config's ``vault_source_backend`` key — mirroring the storage binding's
dispatch contract.

Test IDs follow VSB-NNN (Vault-Source Binding).
"""

import copy
from pathlib import Path

import pytest
import yaml

from sage.config import SageCoreConfig, VaultConfig
from sage.vault_source_binding import (
    VAULT_SOURCE_BACKEND_ENV_VAR,
    DiscoveredVault,
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    build_stack_vault_source_store,
)


def _materialize_vault(root, vault_id, base_config):
    """Write a vault dir under ``root`` with a vault_config.yaml; return its path."""
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    config_path = vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


def test_vsb_001_config_key_selects_the_store(monkeypatch):
    """With the env override unset, ``vault_source_backend: filesystem``
    dispatches to the filesystem store and ``document_store`` to the
    document-store binding.

    Happy path for the selector key.
    """
    monkeypatch.delenv(VAULT_SOURCE_BACKEND_ENV_VAR, raising=False)

    fs = build_stack_vault_source_store(
        SageCoreConfig.model_validate({"vault_source_backend": "filesystem"})
    )
    assert isinstance(fs, FilesystemVaultSourceStore)

    ds = build_stack_vault_source_store(
        SageCoreConfig.model_validate({"vault_source_backend": "document_store"})
    )
    assert isinstance(ds, DocumentStoreVaultSourceStore)


def test_vsb_002_env_override_wins_over_config(monkeypatch):
    """``SAGE_TEST_VAULT_SOURCE_BACKEND`` overrides the config key in both
    directions, and an unrecognized value fails loud.

    Anti-coincidental-pass: a dispatch that read the config before the env would
    fail both directions here while VSB-001 still passed. The unknown-value
    ``ValueError`` ensures a typo'd override cannot silently fall through to the
    configured backend and persist a vault's config to the wrong store.
    """
    monkeypatch.setenv(VAULT_SOURCE_BACKEND_ENV_VAR, "document_store")
    via_env_ds = build_stack_vault_source_store(
        SageCoreConfig.model_validate({"vault_source_backend": "filesystem"})
    )
    assert isinstance(via_env_ds, DocumentStoreVaultSourceStore)

    monkeypatch.setenv(VAULT_SOURCE_BACKEND_ENV_VAR, "filesystem")
    via_env_fs = build_stack_vault_source_store(
        SageCoreConfig.model_validate({"vault_source_backend": "document_store"})
    )
    assert isinstance(via_env_fs, FilesystemVaultSourceStore)

    monkeypatch.setenv(VAULT_SOURCE_BACKEND_ENV_VAR, "sharepoint")
    with pytest.raises(ValueError, match="sharepoint"):
        build_stack_vault_source_store(SageCoreConfig())


def test_vsb_003_filesystem_store_discovers_and_loads(tmp_path, minimal_vault_config_dict):
    """The filesystem store discovers every vault under its root and loads each
    config, with the loaded ``VaultConfig.vault.id`` matching the directory and a
    ``config_path`` pointing at the real on-disk file. An empty or missing root
    yields no vaults.

    Anti-coincidental-pass: assert the loaded id and the on-disk path, not just a
    count — a store that returned empty ``DiscoveredVault``s or wrong ids would
    pass a length-only check.
    """
    root = tmp_path / "vaults"
    _materialize_vault(root, "vault_b", minimal_vault_config_dict)
    _materialize_vault(root, "vault_a", minimal_vault_config_dict)

    store = FilesystemVaultSourceStore(root)
    discovered = store.discover()
    assert [d.config_path.parent.name for d in discovered] == ["vault_a", "vault_b"]  # sorted

    loaded_ids = []
    for d in discovered:
        assert d.config_path.is_file()
        config = store.load_config(d)
        assert isinstance(config, VaultConfig)
        loaded_ids.append(config.vault.id)
    assert loaded_ids == ["vault_a", "vault_b"]

    # Empty and missing roots both discover nothing (no raise).
    assert FilesystemVaultSourceStore(tmp_path / "empty").discover() == []
    (tmp_path / "empty").mkdir()
    assert FilesystemVaultSourceStore(tmp_path / "empty").discover() == []


def test_vsb_004_filesystem_store_write_load_delete_round_trip(tmp_path, minimal_vault_config_dict):
    """``write_config`` persists a config atomically at the located path,
    ``load_config`` reads back an equal ``VaultConfig``, ``config_locator``
    returns that path, and ``delete_config`` removes it (idempotently).

    Anti-coincidental-pass: assert no ``.yaml.tmp`` artifact is left behind, so a
    non-atomic or partial write is caught, and read the value back through
    ``load_config`` rather than just checking the file exists.
    """
    root = tmp_path / "vaults"
    store = FilesystemVaultSourceStore(root)
    vault_id = "round_trip"
    cfg = copy.deepcopy(minimal_vault_config_dict)
    cfg["vault"]["id"] = vault_id

    expected_path = store.config_locator(vault_id)
    assert expected_path == root / vault_id / "vault_config.yaml"

    store.write_config(vault_id, cfg)
    assert expected_path.is_file()
    # No temp-file residue from the atomic write.
    assert list(expected_path.parent.glob("*.yaml.tmp")) == []

    loaded = store.load_config(DiscoveredVault(config_path=expected_path))
    assert loaded.vault.id == vault_id
    assert loaded.vault.name == cfg["vault"]["name"]

    store.delete_config(vault_id)
    assert not expected_path.exists()
    store.delete_config(vault_id)  # idempotent: a second delete does not raise


def test_vsb_005_filesystem_store_rejects_pathless_discovered_vault(tmp_path):
    """``load_config`` on a ``DiscoveredVault`` with no ``config_path`` raises a
    clear ``ValueError`` rather than silently mis-resolving.

    Boundary: the filesystem binding has no way to load a config without a path;
    failing loud here surfaces a binding/caller mismatch instead of an obscure
    ``None``-path error deeper in the loader.
    """
    store = FilesystemVaultSourceStore(tmp_path)
    with pytest.raises(ValueError, match="config_path"):
        store.load_config(DiscoveredVault(config_path=None))


def test_vsb_006_document_store_stub_fails_loud():
    """Every method of the document-store stub raises ``NotImplementedError``
    whose message names the follow-up.

    Anti-coincidental-pass: assert the message names the follow-up (not just that
    *some* ``NotImplementedError`` is raised), so a stub that failed for an
    unrelated reason would not pass.
    """
    store = DocumentStoreVaultSourceStore()
    root = Path("/vault_root")
    for call in (
        lambda: store.discover(),
        lambda: store.load_config(DiscoveredVault(config_path=None)),
        lambda: store.config_locator("v"),
        lambda: store.write_config("v", {}),
        lambda: store.delete_config("v"),
        # Source-byte half: every method fails loud until the cloud adapter lands.
        lambda: store.retain_source("v", root, root / "x.md"),
        lambda: store.source_exists("v", root, "imports/x.md"),
        lambda: store.source_size("v", root, "imports/x.md"),
        lambda: store.read_source("v", root, "imports/x.md"),
        lambda: store.hash_source("v", root, "imports/x.md"),
    ):
        with pytest.raises(NotImplementedError, match="document-store"):
            call()
