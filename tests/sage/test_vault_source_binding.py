"""Tests for the vault-source-store binding of the deployment profiles (CAS-ADR-043).

The binding is a store per backend: the filesystem store reproduces today's
on-disk vault tree (discover/load/write/delete the ``vault_config.yaml``
declaration under the vault root), and the document-store binding persists the
same declaration to a SharePoint library over Microsoft Graph (its own behavior
is exercised in ``test_vault_source_document_store.py``).
``build_stack_vault_source_store`` is the dispatch between them: the
``SAGE_TEST_VAULT_SOURCE_BACKEND`` environment override first, then the stack
config's ``vault_source_backend`` key — mirroring the storage binding's
dispatch contract.

Test IDs follow VSB-NNN (Vault-Source Binding).
"""

import copy
import errno
import shutil

import pytest
import yaml

from sage.config import SageCoreConfig, StackDocumentStoreConfig, VaultConfig
from sage.vault_source_binding import (
    _RMTREE_MAX_ATTEMPTS,
    VAULT_SOURCE_BACKEND_ENV_VAR,
    DiscoveredVault,
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    SupportsSourceDownloadUrl,
    VaultRootEscapeError,
    _strip_macos_ui_artifacts,
    build_stack_vault_source_store,
    remove_tree_tolerating_concurrent_writer,
    resolve_and_assert_within_root,
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


def test_vsb_070_download_url_capability_is_binding_scoped(tmp_path):
    """The optional source-download-URL capability is present only on the
    document-store binding, not the filesystem binding.

    The service probes ``isinstance(store, SupportsSourceDownloadUrl)`` before
    minting a browser download URL, so this pins the probe's two answers: the
    capability stays a richer-binding feature (CAS-ADR-043 s5) and never leaks onto
    the weakest (filesystem) binding.

    Anti-coincidental-pass: assert the filesystem store is NOT an instance *and*
    the document-store binding IS -- a probe that always answered True, or the
    capability accidentally landing on the filesystem store, would fail the first
    assertion.
    """
    fs = FilesystemVaultSourceStore(tmp_path)
    assert not isinstance(fs, SupportsSourceDownloadUrl)

    ds = DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=object())
    assert isinstance(ds, SupportsSourceDownloadUrl)


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


# ---------------------------------------------------------------------------
# delete_source_tree + the root-escape guard (vault teardown, CAS-ADR-043)
# ---------------------------------------------------------------------------


def test_vsb_030_delete_source_tree_removes_the_storage_root(tmp_path):
    """The filesystem binding removes a populated storage_root under its vault root.

    Anti-coincidental-pass: the tree is populated (a positive control that it
    existed), and a sibling vault's tree under the same root is asserted to
    survive -- so a delete that walked the wrong subtree would be caught.
    """
    root = tmp_path / "vaults"
    storage_root = root / "victim" / "sources"
    (storage_root / "imports").mkdir(parents=True)
    (storage_root / "imports" / "a.md").write_text("x")
    sibling = root / "keep" / "sources"
    sibling.mkdir(parents=True)
    (sibling / "b.md").write_text("y")

    FilesystemVaultSourceStore(root).delete_source_tree("victim", storage_root)

    assert not storage_root.exists()
    assert (sibling / "b.md").exists()


def test_vsb_031_delete_source_tree_is_idempotent(tmp_path):
    """Removing an already-absent storage_root is a no-op (does not raise)."""
    root = tmp_path / "vaults"
    root.mkdir()
    store = FilesystemVaultSourceStore(root)
    store.delete_source_tree("gone", root / "gone" / "sources")
    store.delete_source_tree("gone", root / "gone" / "sources")


def test_vsb_032_delete_source_tree_refuses_root_escape(tmp_path):
    """A storage_root that resolves outside the bound vault root is refused and
    nothing is deleted -- including a symlink under the root that escapes it.

    Anti-coincidental-pass: the outside tree is populated and asserted to survive
    both the direct-escape and symlink-escape attempts, so a guard that failed
    open (rmtree ran anyway) would be caught.
    """
    root = tmp_path / "vaults"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete")

    store = FilesystemVaultSourceStore(root)

    with pytest.raises(VaultRootEscapeError):
        store.delete_source_tree("evil", outside)
    assert (outside / "precious.txt").exists()

    link = root / "evil" / "sources"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(VaultRootEscapeError):
        store.delete_source_tree("evil", link)
    assert (outside / "precious.txt").exists()


def test_vsb_033_document_store_delete_source_tree_delegates_to_client_by_id():
    """The document-store binding's ``delete_source_tree`` delegates to the Graph
    client's folder delete, addressing the vault by id and ignoring ``storage_root``
    (None under this binding, which has no filesystem locator).

    Anti-coincidental-pass: assert the client's ``delete_tree`` was called with the
    exact vault id -- a binding that (still) raised, or that reached for the config
    delete or a filesystem path, would fail.
    """

    class _RecordingClient:
        def __init__(self):
            self.deleted = []

        def delete_tree(self, vault_id):
            self.deleted.append(vault_id)

    client = _RecordingClient()
    ds = DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=client)
    ds.delete_source_tree("v", None)
    assert client.deleted == ["v"]


def test_vsb_034_resolve_and_assert_within_root_boundaries(tmp_path):
    """The guard returns the resolved path for a strict descendant, and raises for
    the root itself and for any path outside it.

    Anti-coincidental-pass: the equal-to-root case is the load-bearing one --
    removing the vault root would destroy every vault -- so it must raise, not be
    treated as 'within'.
    """
    root = tmp_path / "vaults"
    (root / "v" / "sources").mkdir(parents=True)

    resolved = resolve_and_assert_within_root(root / "v" / "sources", root)
    assert resolved == (root / "v" / "sources").resolve()

    with pytest.raises(VaultRootEscapeError):
        resolve_and_assert_within_root(root, root)
    with pytest.raises(VaultRootEscapeError):
        resolve_and_assert_within_root(tmp_path / "elsewhere", root)


def test_vsb_071_within_root_guard_speaks_to_two_audiences(tmp_path):
    """The containment guard's message is shaped by who will read it: the
    operator-facing form (no ``display``) names the absolute resolved path and
    the root it was checked against, while the caller-facing form names only
    the vault-relative spelling the caller supplied.

    Anti-coincidental-pass: the two halves fail in opposite directions, so a
    guard collapsed onto either spelling fails one of them. Always-relative
    fails the operator assertions -- and would leave a vault teardown's receipt
    unable to say which root it checked. Always-absolute, or a form that names
    the display *and* still appends the root, fails the caller half's negative
    assertions. Both absolute strings are taken in resolved form, since that is
    what the message prints. Neither path is created: the guard resolves
    non-strictly, so the tree's existence is not part of what it decides on.
    """
    root = tmp_path / "vaults"
    outside = tmp_path / "elsewhere"

    with pytest.raises(VaultRootEscapeError) as operator:
        resolve_and_assert_within_root(outside, root)
    operator_message = str(operator.value)
    assert str(outside.resolve()) in operator_message
    assert str(root.resolve()) in operator_message

    with pytest.raises(VaultRootEscapeError) as caller:
        resolve_and_assert_within_root(outside, root, display="imports/x.md")
    caller_message = str(caller.value)
    assert "imports/x.md" in caller_message
    assert str(outside.resolve()) not in caller_message
    assert str(root.resolve()) not in caller_message


# ---------------------------------------------------------------------------
# remove_tree_tolerating_concurrent_writer: rmtree resilient to a concurrent
# writer repopulating a directory mid-removal (macOS .DS_Store precedent,
# CAS-ADR-016)
# ---------------------------------------------------------------------------


def test_vsb_035_remove_tree_removes_a_populated_tree(tmp_path):
    """The resilient remove deletes a populated tree; a sibling tree survives.

    Anti-coincidental-pass: the tree is populated (a positive control that it
    existed) and a sibling under the same parent is asserted to survive, so a
    remove that walked the wrong subtree would be caught.
    """
    victim = tmp_path / "victim"
    (victim / "a").mkdir(parents=True)
    (victim / "a" / "f.txt").write_text("x")
    sibling = tmp_path / "keep"
    sibling.mkdir()
    (sibling / "b.txt").write_text("y")

    remove_tree_tolerating_concurrent_writer(victim)

    assert not victim.exists()
    assert (sibling / "b.txt").exists()


def test_vsb_036_remove_tree_retries_a_transient_enotempty(tmp_path, monkeypatch):
    """A concurrent writer that repopulates a directory between rmtree's scan and
    its final rmdir (ENOTEMPTY) is tolerated: the removal retries and completes.

    Anti-coincidental-pass: reproduces the race the fix targets. The first rmtree
    re-creates a .DS_Store inside the tree and raises ENOTEMPTY; only the retry
    completes the removal, so a single-attempt remove would leave the tree.
    """
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "sub" / "f.txt").write_text("x")

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # The concurrent writer: a .DS_Store reappears, so the final rmdir
            # would see a non-empty directory.
            (victim / "sub" / ".DS_Store").write_text("finder")
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(victim / "sub"))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky)
    monkeypatch.setattr("sage.vault_source_binding._RMTREE_RETRY_BACKOFF_SECONDS", 0)

    remove_tree_tolerating_concurrent_writer(victim)

    assert calls["n"] == 2
    assert not victim.exists()


def test_vsb_037_strip_macos_ui_artifacts_removes_ds_store(tmp_path):
    """`_strip_macos_ui_artifacts` deletes .DS_Store files at every level and keeps
    real content.

    Anti-coincidental-pass: `keep.md` is a positive control -- a strip that walked
    too broadly (deleted more than .DS_Store) would remove it.
    """
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / ".DS_Store").write_text("finder")
    (root / "a" / ".DS_Store").write_text("finder")
    (root / "a" / "keep.md").write_text("body")

    _strip_macos_ui_artifacts(root)

    assert not (root / ".DS_Store").exists()
    assert not (root / "a" / ".DS_Store").exists()
    assert (root / "a" / "keep.md").exists()


def test_vsb_038_remove_tree_propagates_a_non_transient_oserror(tmp_path, monkeypatch):
    """A non-transient OSError (EACCES) is not swallowed or retried: it propagates
    on the first attempt.

    Anti-coincidental-pass: asserting a single call proves the helper does not treat
    every OSError as a retryable race -- an over-broad except would loop and change
    the call count.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    calls = {"n": 0}

    def eacces(path, *args, **kwargs):
        calls["n"] += 1
        raise OSError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", eacces)

    with pytest.raises(OSError) as exc_info:
        remove_tree_tolerating_concurrent_writer(victim)

    assert exc_info.value.errno == errno.EACCES
    assert calls["n"] == 1


def test_vsb_039_remove_tree_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    """A writer that never stops (persistent ENOTEMPTY) does not hang the removal:
    it gives up after a bounded number of attempts and raises.

    Anti-coincidental-pass: asserting an exact call count proves the retry loop is
    bounded -- an unbounded loop would never return to be counted.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    calls = {"n": 0}

    def always_enotempty(path, *args, **kwargs):
        calls["n"] += 1
        raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

    monkeypatch.setattr(shutil, "rmtree", always_enotempty)
    monkeypatch.setattr("sage.vault_source_binding._RMTREE_RETRY_BACKOFF_SECONDS", 0)

    with pytest.raises(OSError) as exc_info:
        remove_tree_tolerating_concurrent_writer(victim)

    assert exc_info.value.errno == errno.ENOTEMPTY
    assert calls["n"] == _RMTREE_MAX_ATTEMPTS


def test_vsb_040_remove_tree_is_idempotent_on_an_absent_path(tmp_path):
    """Removing an already-absent path is a silent no-op (does not raise)."""
    remove_tree_tolerating_concurrent_writer(tmp_path / "does-not-exist")


# --------------------------------------------------------------------------
# Store lifecycle: close() releases the document-store binding's Graph client
# --------------------------------------------------------------------------


class _RecordingGraphClient:
    """A Graph-client stand-in that records its close."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_vsb_050_document_store_close_closes_client():
    """``DocumentStoreVaultSourceStore.close`` closes the Graph client it holds
    (built eagerly under managed identity, or injected for tests).

    Anti-coincidental-pass: the recording client flips ``closed`` only if the store
    delegates to its client's ``close()`` -- a store ``close`` that ignored the
    client would leave it False.
    """
    client = _RecordingGraphClient()
    store = DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=client)

    store.close()

    assert client.closed is True


def test_vsb_051_document_store_close_noop_when_client_unbuilt():
    """A lazily-bound store that never built its client holds no transport, so
    ``close`` is a no-op and does not construct one.

    Anti-coincidental-pass: assert the client cache stays ``None`` after close -- a
    close that called ``_get_client()`` would eagerly build (and try to reach
    Azure) exactly when there is nothing to release.
    """
    store = DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=None)

    store.close()  # must not raise, must not build

    assert store._client is None


def test_vsb_052_filesystem_store_close_is_noop(tmp_path):
    """The filesystem binding holds no client, so it inherits the port's no-op
    ``close`` -- callers can close any binding without branching on the backend."""
    store = FilesystemVaultSourceStore(tmp_path)

    store.close()  # must not raise
