"""Tests for the shared vault-source-binding selector in ``tests/helpers``.

``select_vault_source_binding`` is the one place a test pins the stack's
vault-source binding (CAS-ADR-043) and wires the fake Graph transport behind the
document-store leg. Every suite that needs a binding-parameterized server or
store calls it, so these tests assert its contract directly rather than leaving
it to be inferred from the many tests that use it: a construction path the
helper stops reaching surfaces here, once, instead of silently in whichever
caller still passes without the fake.

Test IDs follow VSS-NNN (Vault-Source Selection).
"""

import os
import typing

import pytest

import sage.vault_source_document_store as document_store_module
from sage.config import SageCoreConfig
from sage.vault_source_binding import (
    VAULT_SOURCE_BACKEND_ENV_VAR,
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    build_stack_vault_source_store,
)
from tests.helpers.vault_source_selection import (
    WIRED_BACKENDS,
    select_vault_source_binding,
)


def test_vss_001_document_store_leg_routes_dispatch_through_fake(monkeypatch, tmp_path):
    """VSS-001: the document-store leg reaches the returned fake end to end.

    The default config selects the filesystem binding, so only the env override
    can send dispatch to the document store; and the lazily-built client comes
    from the patched factory, so only the fake can answer ``discover`` with the
    seeded vault. A store that never touched the fake cannot produce it.

    A second selection must hand out a fresh fake and move the factory to it.
    A selector returning one process-global fake would pass the first half and
    leak state -- seeded configs, installed refusals -- from test to test; the
    second call's identity check and its empty discovery exclude that rival.
    """
    fake = select_vault_source_binding(monkeypatch, "document_store")
    assert fake is not None
    fake.store["probe_vault"] = b"vault:\n  id: probe_vault\n"

    store = build_stack_vault_source_store(SageCoreConfig(), vault_root=tmp_path)
    discovered = store.discover()

    assert isinstance(store, DocumentStoreVaultSourceStore)
    assert [d.vault_id for d in discovered] == ["probe_vault"]
    assert all(d.config_path is None for d in discovered)

    second = select_vault_source_binding(monkeypatch, "document_store")
    assert second is not fake
    rebuilt = build_stack_vault_source_store(SageCoreConfig(), vault_root=tmp_path)
    assert rebuilt.discover() == []


def test_vss_002_filesystem_leg_overrides_config_and_wires_nothing(monkeypatch, tmp_path):
    """VSS-002: the filesystem leg pins the filesystem binding and patches nothing.

    The config selects the document store, so the filesystem store resolves only
    if the helper actually set the env override -- under the default config a
    helper that did nothing would pass by accident.
    """
    original_factory = document_store_module.build_sharepoint_graph_client

    result = select_vault_source_binding(monkeypatch, "filesystem")
    store = build_stack_vault_source_store(
        SageCoreConfig(vault_source_backend="document_store"), vault_root=tmp_path
    )

    assert result is None
    assert isinstance(store, FilesystemVaultSourceStore)
    assert document_store_module.build_sharepoint_graph_client is original_factory


@pytest.mark.parametrize("backend", ["sharepoint", "", "Filesystem"])
def test_vss_003_unknown_backend_refused_without_side_effects(monkeypatch, backend):
    """VSS-003: an unknown backend is refused before anything is set or patched.

    The ``delenv`` precondition guards against an override inherited from the
    ambient environment, which would otherwise make the env-unset assertion
    fail for a reason unrelated to the helper.
    """
    monkeypatch.delenv(VAULT_SOURCE_BACKEND_ENV_VAR, raising=False)
    original_factory = document_store_module.build_sharepoint_graph_client

    with pytest.raises(ValueError, match=f"backend {backend!r}"):
        select_vault_source_binding(monkeypatch, backend)

    assert VAULT_SOURCE_BACKEND_ENV_VAR not in os.environ
    assert document_store_module.build_sharepoint_graph_client is original_factory


def test_vss_004_wired_backends_match_config_selector():
    """VSS-004: the helper wires exactly the backends the stack config admits.

    Compared against the config field's live ``Literal`` arguments, so a backend
    added to the selector fails here until the helper is revisited. This pins the
    declared set only: a backend appended to ``WIRED_BACKENDS`` without a wiring
    branch of its own would pass, and its wiring needs a test of the VSS-001 shape.
    """
    annotation = SageCoreConfig.model_fields["vault_source_backend"].annotation
    assert set(WIRED_BACKENDS) == set(typing.get_args(annotation))
