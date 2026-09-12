"""Signature contract for the provider, source-adapter, and provisioner ports.

Each of these ports is an ABC with several bindings a service can be handed
interchangeably: real model providers beside the hermetic stubs services are
tested on, one source adapter per format behind a single registry, and the
storage provisioner behind its port. A binding whose signature admits a call
the port forbids lets a caller exercise a shape another binding would reject,
so every port method's signature is compared against every binding.

The storage ports carry their own seam modules
(tests/sage/test_graph_store_seam.py, tests/sage/test_content_store_seam.py,
tests/sage/test_vault_source_store_seam.py); this module covers the rest.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import sage.adapters
import sage.source_adapters
from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider
from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
from sage.adapters.embedding_nomic import NomicEmbeddingProvider
from sage.adapters.interfaces import AbstractionProvider, EmbeddingProvider
from sage.adapters.stubs import (
    FailingAbstractionProvider,
    SeededEmbeddingProvider,
    StubAbstractionProvider,
    StubEmbeddingProvider,
)
from sage.source_adapters.base import SourceAdapter
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.pptx_adapter import PptxAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from sage.storage_binding import PostgresVaultStorageProvisioner, VaultStorageProvisioner
from tests.helpers.seam_signatures import (
    assert_signature_conforms,
    parametrized_values,
    port_surface,
)

_PORT_BINDINGS: dict[type, list[type]] = {
    EmbeddingProvider: [NomicEmbeddingProvider, SeededEmbeddingProvider, StubEmbeddingProvider],
    AbstractionProvider: [
        AnthropicAbstractionProvider,
        FailingAbstractionProvider,
        Qwen3AbstractionProvider,
        StubAbstractionProvider,
    ],
    SourceAdapter: [DocxAdapter, MarkdownAdapter, PdfAdapter, PptxAdapter, XlsxAdapter],
    VaultStorageProvisioner: [PostgresVaultStorageProvisioner],
}

# Where bindings of the ports above live. Every module here is walked by the
# completeness test, so a binding added in a new module is found, not assumed.
_BINDING_PACKAGES = (sage.adapters, sage.source_adapters)
_BINDING_MODULES = ("sage.storage_binding",)

# (binding, method) pairs whose signature is known to differ from the port's.
# Deliberately empty: every binding carries its port's signatures exactly. A
# drift belongs fixed in the binding; pinning one here needs a reason beside
# it, and a pin whose drift is later fixed fails the gate, so the set can only
# shrink.
KNOWN_SIGNATURE_DIVERGENCES: frozenset[tuple[type, str]] = frozenset()

_CASES = [
    pytest.param(port, binding, method_name, id=f"{binding.__name__}-{method_name}")
    for port, bindings in _PORT_BINDINGS.items()
    for binding in bindings
    for method_name in sorted(port_surface(port))
]


def _discovered_bindings(port: type) -> set[type]:
    """Concrete subclasses of ``port`` defined in the walked binding modules.

    Read from each module's own namespace rather than from
    ``port.__subclasses__()``, so what is found depends on the walk and not on
    whatever this file, or another test in the process, happened to import.
    """
    names = [
        info.name
        for package in _BINDING_PACKAGES
        for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
    ]
    names.extend(_BINDING_MODULES)
    found: set[type] = set()
    for name in names:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == name
                and issubclass(value, port)
                and not inspect.isabstract(value)
            ):
                found.add(value)
    return found


@pytest.mark.parametrize("port, binding, method_name", _CASES)
def test_binding_signature_matches_port(port, binding, method_name):
    """Each binding method's signature matches its port's exactly.

    Trap: a parameter rename, default change, or return-type drift between a
    port and one of its bindings breaks substitutability silently -- nothing
    else compares them, and the bindings a test suite stands services on are
    the stubs, not the providers production runs.
    """
    assert_signature_conforms(port, binding, method_name, divergences=KNOWN_SIGNATURE_DIVERGENCES)


def test_signature_gate_covers_every_declared_binding_and_port_method():
    """The gate is parametrized over every port method of every declared binding.

    Trap: a case list narrowed to the abstract set, or built from something
    other than the declaration, keeps the gate green while part of a seam goes
    unchecked. The cases are read back from the collected parametrization. A
    binding missing from the declaration itself is the next test's to catch.
    """
    collected = {
        tuple(case.values)
        for case in parametrized_values(
            test_binding_signature_matches_port, "port, binding, method_name"
        )
    }
    expected = {
        (port, binding, method_name)
        for port, bindings in _PORT_BINDINGS.items()
        for binding in bindings
        for method_name in port_surface(port)
    }
    assert collected == expected


def test_every_sage_binding_is_declared():
    """Every concrete binding of these ports in the binding modules is under the gate.

    The walk covers ``_BINDING_PACKAGES`` and ``_BINDING_MODULES``; a binding
    placed outside them is outside this guard, which is why those name where
    bindings live rather than where they happen to be today.

    Trap: a new provider or source adapter added without a ``_PORT_BINDINGS``
    entry is never compared and the gate stays green. A declared entry that is
    abstract, or not a subclass of the port it is listed under, fails too.
    """
    for port, declared in _PORT_BINDINGS.items():
        for binding in declared:
            assert issubclass(binding, port), f"{binding.__name__} is not a {port.__name__}"
        concrete = _discovered_bindings(port)
        assert concrete == set(declared), (
            f"{port.__name__}: undeclared {sorted(c.__name__ for c in concrete - set(declared))}, "
            f"not concrete {sorted(c.__name__ for c in set(declared) - concrete)}"
        )


@pytest.mark.parametrize("port", list(_PORT_BINDINGS), ids=lambda p: p.__name__)
def test_binding_signature_mutation_turns_the_gate_red(port, monkeypatch):
    """A deliberate drift on a real binding fails the gate it runs through.

    Without this probe a gate that silently compared a method to itself would
    pass for the wrong reason. The mutant adds a required keyword-only
    parameter, a shape no port method carries.
    """
    binding = _PORT_BINDINGS[port][0]
    method_name = sorted(port.__abstractmethods__)[0]

    def mutant(self, *, drift_probe: int):
        return None

    monkeypatch.setattr(binding, method_name, mutant)
    with pytest.raises(AssertionError, match=f"{binding.__name__}.{method_name}"):
        test_binding_signature_matches_port(port, binding, method_name)
