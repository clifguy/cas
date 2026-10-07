"""Relative ingest sources stay inside the vault's storage root.

A relative ``source`` names a location in the vault's source tree. Before it is
read, hashed or retained it is resolved and refused unless it lands strictly
under ``storage_root`` -- on the real ingest and on a dry run alike, so neither
reports anything about a file outside the tree. Under a vault-source binding
with no local tree the local-disk branch is not consulted at all: a relative
source is answered by the store or not at all.

Each test runs once per vault-source binding via ``vault_source_backend``.
"""

import contextlib
import json
import os
from pathlib import Path

import pytest

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_server import ingest_document
from tests.sage.conftest import initialize_services_for_test

_VAULT_ID = "test_vault"


def _parse(result: str | dict) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


@contextlib.contextmanager
def _profile(name: str):
    saved = _mcp_init._stack_config
    _mcp_init.set_stack_config(SageCoreConfig(profile=name))
    try:
        yield
    finally:
        _mcp_init.set_stack_config(saved)


@pytest.fixture
async def vault(minimal_vault_config_dict, vault_source_backend):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults[_VAULT_ID] = services
        try:
            yield services, config, vault_source_backend
        finally:
            _mcp._vaults.pop(_VAULT_ID, None)


def _outside_file(config: VaultConfig) -> Path:
    """A readable file beside, not under, the vault's storage root."""
    storage_root = Path(config.vault.storage_root)
    outside = storage_root.parent / "outside.md"
    outside.write_text("# Outside\n\nNot part of the vault.\n")
    return outside


def _retained_names(handle, storage_root: str) -> list[str]:
    """Every retained source path on the active leg."""
    if handle.fake_client is not None:
        return sorted(handle.fake_client.sources)
    root = Path(storage_root)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("profile", ["local", "cloud"])
async def test_relative_source_escaping_the_storage_root_is_refused(vault, profile):
    """TEST-SAGE-BH-149: a ``..`` source naming a real file outside the
    tree is refused with ``vault_source_path_refused`` and nothing is retained.

    The outside file exists and is readable, so without the containment check
    the filesystem leg ingests it (copying it into ``imports/``) and the
    document-store leg uploads it -- the refusal is what proves the check ran.
    """
    services, config, handle = vault
    _outside_file(config)

    with _profile(profile):
        result = _parse(await ingest_document(_VAULT_ID, "../outside.md", "markdown"))

    assert result.get("error") == "vault_source_path_refused", result
    assert not any("outside" in name for name in _retained_names(handle, config.vault.storage_root))


@pytest.mark.parametrize("dry_run", [False, True])
async def test_escape_refusal_does_not_depend_on_existence(vault, dry_run):
    """TEST-SAGE-BH-150: an escaping source is refused whether or not the
    target exists, on the real ingest and the dry run alike.

    A refusal that fired only for a present file, or a dry run answering
    ``source_file_not_found`` for an absent one, would let a caller probe the
    host filesystem by comparing answers.
    """
    _services, config, _handle = vault
    _outside_file(config)

    present = _parse(await ingest_document(_VAULT_ID, "../outside.md", "markdown", dry_run=dry_run))
    absent = _parse(
        await ingest_document(_VAULT_ID, "../not-there.md", "markdown", dry_run=dry_run)
    )

    assert present.get("error") == "vault_source_path_refused", present
    assert absent.get("error") == "vault_source_path_refused", absent
    assert "source_content_hash" not in json.dumps(present)


async def test_symlink_inside_the_tree_resolving_outside_is_refused(vault):
    """TEST-SAGE-BH-151: a link under ``storage_root`` whose target lies
    outside is refused on the binding that reads the local tree.

    The lexical path is inside the tree, so a check on the spelling alone
    passes it; only the resolved target shows the escape.
    """
    _services, config, handle = vault
    if handle.backend != "filesystem":
        pytest.skip("only the filesystem binding reads the local tree")
    outside = _outside_file(config)
    link = Path(config.vault.storage_root) / "linked.md"
    os.symlink(outside, link)

    result = _parse(await ingest_document(_VAULT_ID, "linked.md", "markdown"))

    assert result.get("error") == "vault_source_path_refused", result


async def test_relative_source_inside_the_tree_still_ingests(vault):
    """TEST-SAGE-BH-152 (control): a plain relative source inside the tree
    ingests on the filesystem binding and is answered by the store on the
    document-store binding.

    The positive arm of BH-149: a containment check that refused every
    relative source would pass the refusal tests and fail here.
    """
    _services, config, handle = vault
    inside = Path(config.vault.storage_root) / "docs" / "inside.md"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("# Inside\n\nPart of the vault.\n")
    if handle.backend == "document_store":
        storage_root = config.vault.storage_root
        handle.write_retained_bytes(storage_root, "docs/inside.md", inside.read_bytes())

    result = _parse(await ingest_document(_VAULT_ID, "docs/inside.md", "markdown"))

    assert "error" not in result, result
    assert result["source_path"] == "docs/inside.md"


async def test_store_without_a_local_tree_ignores_local_disk(vault):
    """TEST-SAGE-BH-153: under the document-store binding a relative source
    present only on the process's local disk is not found.

    The same file is ingested on the filesystem binding, which keeps its
    sources on that disk -- the pair shows the local branch is skipped only
    where the binding has no local tree.
    """
    _services, config, handle = vault
    local_only = Path(config.vault.storage_root) / "local-only.md"
    local_only.write_text("# Local only\n\nOn this disk, not in the store.\n")

    result = _parse(await ingest_document(_VAULT_ID, "local-only.md", "markdown"))

    if handle.backend == "document_store":
        assert result.get("error") == "source_file_not_found", result
    else:
        assert "error" not in result, result
