"""Signature contract for the backend-for-frontend's ports.

The BFF reaches SAGE through a ``SageTransport`` port (over HTTP to a separate
process, or in process under the standalone profile) and keeps sessions behind
a ``SessionStore`` port (in memory, or durable in Postgres). A binding whose
signature admits a call the port forbids works under one profile and fails
under the other, so every port method's signature is compared against every
binding.

The transport module names ``Session`` in its annotations but imports it only
for type checkers, to avoid an import cycle with the session store, so the gate
supplies that one name explicitly.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import app.backend
from app.backend.auth.session_store import (
    InMemorySessionStore,
    PostgresSessionStore,
    Session,
    SessionStore,
)
from app.backend.transport import HttpSageTransport, InProcessSageTransport, SageTransport
from tests.helpers.seam_signatures import (
    assert_signature_conforms,
    parametrized_values,
    port_surface,
)

_PORT_BINDINGS: dict[type, list[type]] = {
    SageTransport: [HttpSageTransport, InProcessSageTransport],
    SessionStore: [InMemorySessionStore, PostgresSessionStore],
}

_ANNOTATION_NAMES = {"Session": Session}

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
    """Concrete subclasses of ``port`` defined anywhere in the backend package.

    Read from each module's own namespace rather than from
    ``port.__subclasses__()``, so what is found depends on the walk and not on
    whatever this file, or another test in the process, happened to import.
    """
    found: set[type] = set()
    for info in pkgutil.walk_packages(app.backend.__path__, f"{app.backend.__name__}."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == info.name
                and issubclass(value, port)
                and not inspect.isabstract(value)
            ):
                found.add(value)
    return found


@pytest.mark.parametrize("port, binding, method_name", _CASES)
def test_binding_signature_matches_port(port, binding, method_name):
    """Each binding method's signature matches its port's exactly.

    Trap: a parameter rename, default change, or return-type drift between a
    port and one binding breaks only the profile that selects that binding,
    and nothing else compares them.
    """
    assert_signature_conforms(
        port,
        binding,
        method_name,
        divergences=KNOWN_SIGNATURE_DIVERGENCES,
        annotation_names=_ANNOTATION_NAMES,
    )


def test_signature_gate_covers_every_declared_binding_and_port_method():
    """The gate is parametrized over every port method of every declared binding.

    Trap: a case list narrowed to the abstract set, or built from something
    other than the declaration, keeps the gate green while part of a seam goes
    unchecked -- today every method on both ports is abstract, so the first
    shape bites only once a port gains a defaulted method. A binding missing
    from the declaration itself is the next test's to catch.
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


def test_every_backend_binding_is_declared():
    """Every concrete binding of these ports in the backend is under the gate.

    Trap: a new transport or session store added without a ``_PORT_BINDINGS``
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
