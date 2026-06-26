"""VaultSourceStore adapter-seam contract: ABC, filesystem binding, stub.

Proves the vault-source store is a real swappable port (CAS-ADR-043): a
``VaultSourceStore`` ABC, a concrete ``FilesystemVaultSourceStore``, and a
``DocumentStoreVaultSourceStore`` stub that fully implements the port surface
(every method present, raising until the cloud adapter lands). The structural
tests (T1-T5) guard that surface so a method added to the port cannot be
silently omitted from a binding, and a signature on the source-byte half cannot
drift between the port and either binding.

The dispatch / substitutability behavior (env override vs config key) is covered
by tests/sage/test_vault_source_binding.py (VSB-001/002); this module is the
structural mirror of tests/sage/test_graph_store_seam.py.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from sage.config import StackDocumentStoreConfig
from sage.vault_source_binding import (
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    VaultSourceStore,
)

# Methods whose concrete return type legitimately narrows the port's, sanctioned
# by CAS-ADR-043 ("richer-binding capabilities live inside the binding"): the
# filesystem binding always has a config path, so it returns ``Path`` where the
# port returns ``Path | None``. For these, parameter parity is asserted but the
# return annotation is exempt. Keyed by (binding, method).
_RETURN_NARROWED = {(FilesystemVaultSourceStore, "config_locator")}

_BINDINGS = [FilesystemVaultSourceStore, DocumentStoreVaultSourceStore]


def _public_methods(cls: type) -> set[str]:
    """Public methods defined directly on ``cls`` (not inherited)."""
    return {
        name
        for name, val in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(val)
    }


# --------------------------------------------------------------------------- #
# Structural contract (T1-T5)
# --------------------------------------------------------------------------- #


def test_vss_t1_port_is_abstract():
    """T1: VaultSourceStore is a genuine ABC and cannot be instantiated.

    Trap: were it a plain class with no @abstractmethod members,
    ``VaultSourceStore()`` would succeed and the seam would be a port in name
    only."""
    assert inspect.isabstract(VaultSourceStore)
    assert len(VaultSourceStore.__abstractmethods__) > 0
    with pytest.raises(TypeError):
        VaultSourceStore()  # type: ignore[abstract]


def test_vss_t2_filesystem_binding_is_concrete():
    """T2: FilesystemVaultSourceStore implements the full port (no leftovers).

    Trap: a single un-implemented abstract method flips isabstract back to True
    and makes the class un-instantiable."""
    assert issubclass(FilesystemVaultSourceStore, VaultSourceStore)
    assert not inspect.isabstract(FilesystemVaultSourceStore)
    FilesystemVaultSourceStore(Path("/unused"))  # must not raise


def test_vss_t3_document_store_stub_is_concrete():
    """T3: DocumentStoreVaultSourceStore implements the full port and instantiates.

    Trap: a stub that silently omitted a method would stay abstract; the
    instantiation below would raise."""
    assert issubclass(DocumentStoreVaultSourceStore, VaultSourceStore)
    assert not inspect.isabstract(DocumentStoreVaultSourceStore)
    DocumentStoreVaultSourceStore(StackDocumentStoreConfig())  # must not raise


@pytest.mark.parametrize("binding", _BINDINGS)
def test_vss_t4_binding_surface_matches_port(binding):
    """T4: each binding's public surface is exactly the port's abstract set.

    Trap: a binding-private public method (drift) would appear in the difference,
    and a port method dropped from a binding would too. The port has no
    binding-private public methods, so the sets must be equal."""
    abc_methods = set(VaultSourceStore.__abstractmethods__)
    assert _public_methods(binding) == abc_methods


@pytest.mark.parametrize("binding", _BINDINGS)
@pytest.mark.parametrize("method_name", sorted(VaultSourceStore.__abstractmethods__))
def test_vss_t5_binding_signature_matches_port(binding, method_name):
    """T5: each binding method's signature matches the port's.

    Trap: a parameter rename, default change, or annotation drift on the
    source-byte half would break substitutability silently while T2-T4 stay
    green. The one sanctioned divergence (covariant return narrowing on
    ``config_locator``) is checked at parameter level only."""
    port_sig = inspect.signature(getattr(VaultSourceStore, method_name))
    binding_sig = inspect.signature(getattr(binding, method_name))

    if (binding, method_name) in _RETURN_NARROWED:
        assert binding_sig.parameters == port_sig.parameters, (
            f"{binding.__name__}.{method_name}: parameters "
            f"{binding_sig.parameters} != port {port_sig.parameters}"
        )
    else:
        assert binding_sig == port_sig, (
            f"{binding.__name__}.{method_name}: {binding_sig} != port {port_sig}"
        )
