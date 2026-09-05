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

import copy
import inspect
import logging
from pathlib import Path

import pytest

from sage.config import StackDocumentStoreConfig
from sage.services.vault_source_errors import _TranslatingVaultSourceStore
from sage.vault_source_binding import (
    DocumentStoreVaultSourceStore,
    FilesystemVaultSourceStore,
    SupportsSourceDownloadUrl,
    VaultSourceStore,
)
from tests.helpers.fake_graph_client import FakeGraphClient

# Methods whose concrete return type legitimately narrows the port's, sanctioned
# by CAS-ADR-043 ("richer-binding capabilities live inside the binding"): the
# filesystem binding always has a config path, so it returns ``Path`` where the
# port returns ``Path | None``. For these, parameter parity is asserted but the
# return annotation is exempt. Keyed by (binding, method).
_RETURN_NARROWED = {(FilesystemVaultSourceStore, "config_locator")}

# The translating wrapper is a binding by the same contract: it is what every
# caller that resolves the store actually holds, and it stands in front of both
# concrete bindings. Included here rather than tested separately because T4/T5
# are what make its coverage of the port structural -- a source-byte method
# added to the port that the wrapper does not install fails T4 by name, which
# is the property that let the hand-placement gate be deleted (CAS-ADR-043).
# Its download-URL variant is deliberately absent: T4 reads methods declared
# directly on a class, and that subclass declares only the capability method
# and inherits the port surface pinned here. Its capability scoping is pinned
# by tests/sage/test_vault_source_store_translation.py instead.
_BINDINGS = [
    FilesystemVaultSourceStore,
    DocumentStoreVaultSourceStore,
    _TranslatingVaultSourceStore,
]


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


# Optional richer-binding capabilities (CAS-ADR-043 s5): capability protocols a
# binding MAY implement on top of the port. Their methods are a legitimate part of
# a binding's public surface but are deliberately absent from the port ABC, which
# stays satisfiable by its weakest binding. Keyed by binding class; the allowed
# method names are derived from the protocol so there is one source of truth.
_OPTIONAL_CAPABILITIES: dict[type, tuple[type, ...]] = {
    DocumentStoreVaultSourceStore: (SupportsSourceDownloadUrl,),
}

# Concrete port methods: non-abstract public methods the ABC defines with a
# default (lifecycle helpers such as ``close``). They are part of the sanctioned
# surface whether a binding inherits the default (absent from its direct methods)
# or overrides it (present) -- so the binding's surface is bounded above by the
# full sanctioned set, not required to equal it. Derived, so a new concrete port
# method joins automatically.
_CONCRETE_PORT_METHODS = _public_methods(VaultSourceStore) - set(
    VaultSourceStore.__abstractmethods__
)


@pytest.mark.parametrize("binding", _BINDINGS)
def test_vss_t4_binding_surface_matches_port(binding):
    """T4: each binding's public surface is the port's abstract set, plus any
    optional richer-binding capability protocol it implements (CAS-ADR-043 s5),
    plus any concrete port lifecycle method it may override.

    Trap: a binding-private public method that is NOT part of the port surface, a
    declared optional capability, or a concrete port method (drift) would fall
    outside ``allowed``; and a port method dropped from a binding would leave an
    abstract method unimplemented."""
    allowed = set(VaultSourceStore.__abstractmethods__) | _CONCRETE_PORT_METHODS
    for proto in _OPTIONAL_CAPABILITIES.get(binding, ()):
        allowed |= _public_methods(proto)
    surface = _public_methods(binding)
    # Drift guard: every public method the binding exposes is sanctioned.
    assert surface <= allowed, f"unsanctioned public methods: {sorted(surface - allowed)}"
    # Completeness: every abstract port method is implemented directly on the binding.
    assert set(VaultSourceStore.__abstractmethods__) <= surface


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


# --------------------------------------------------------------------------- #
# Behavioral contract (B1-)
#
# Obligations every binding must honor identically, asserted once against the
# port surface and parameterized over the concrete bindings, so a future
# binding inherits them for free. The structural tests above cannot see a
# method that exists on both bindings but behaves differently; this section
# exists to catch exactly that divergence.
# --------------------------------------------------------------------------- #

# A retired config section in the shape the migration warning targets
# (CAS-ADR-046). Its content is irrelevant to the warning -- presence of the
# key is the trigger -- but a realistic shape keeps the staged config honest.
_RETIRED_LEGACY_SECTION = {
    "adapters": [
        {
            "source_type": "docx",
            "enabled": True,
            "config": {"file_extensions": [".docx"]},
        },
    ],
}


@pytest.fixture(params=["filesystem", "document_store"])
def source_store(request, tmp_path):
    """One constructed binding per param, driven through the port surface only.

    Filesystem binds to a tmp vault root; document-store binds to the shared
    in-memory fake Graph client. Tests stage state via ``write_config`` and
    read it back via ``discover`` / ``load_config`` so the same test body
    exercises both bindings without binding-specific branches.
    """
    if request.param == "filesystem":
        return FilesystemVaultSourceStore(tmp_path)
    return DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=FakeGraphClient())


def _discovered_by_id(store: VaultSourceStore, vault_id: str):
    matches = [d for d in store.discover() if d.vault_id == vault_id]
    assert len(matches) == 1, f"expected exactly one discovered vault {vault_id!r}"
    return matches[0]


def test_vss_b1_load_config_warns_on_retired_section(
    source_store, minimal_vault_config_dict, caplog
):
    """B1: ``load_config`` on a stored declaration still carrying a retired
    section emits the migration WARNING naming the vault; a clean declaration
    loads silently. Both bindings, one obligation (CAS-ADR-046: the warning is
    what keeps 'inert' from meaning 'invisible' to the operator).

    Trap: the clean-config control kills an unconditional warn-on-every-load;
    the exactly-one assertion kills a double-emit (helper plus a nested
    ``load_vault_config``); the vault-id-in-message assertion kills a
    hardcoded log line. A binding that validates the raw mapping without
    routing it past ``warn_on_retired_sections`` fails the stale half while
    the other binding passes -- the exact divergence this section exists to
    surface."""
    clean = copy.deepcopy(minimal_vault_config_dict)
    clean["vault"]["id"] = "clean_vault"
    source_store.write_config("clean_vault", clean)

    stale = copy.deepcopy(minimal_vault_config_dict)
    stale["vault"]["id"] = "stale_vault"
    stale["source_adapters"] = _RETIRED_LEGACY_SECTION
    source_store.write_config("stale_vault", stale)

    with caplog.at_level(logging.WARNING, logger="sage.config"):
        source_store.load_config(_discovered_by_id(source_store, "clean_vault"))
    assert [r for r in caplog.records if "source_adapters" in r.getMessage()] == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = source_store.load_config(_discovered_by_id(source_store, "stale_vault"))

    matching = [r for r in caplog.records if "source_adapters" in r.getMessage()]
    assert len(matching) == 1
    message = matching[0].getMessage()
    assert "is retired and ignored" in message
    assert "stale_vault" in message
    # The parsed model drops the section either way; the warning is the only
    # surviving trace of it.
    assert getattr(config, "source_adapters", None) is None
